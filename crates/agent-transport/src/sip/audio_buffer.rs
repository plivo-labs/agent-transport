//! Shared audio buffer matching WebRTC C++ AudioSource's internal buffer.
//!
//! Architecture:
//! - `send_audio` pushes samples under mutex, checks threshold
//! - If below threshold: signals completion immediately
//! - If above threshold: stores completion signal for deferred firing
//! - RTP send loop drains samples under same mutex every 20ms
//! - After draining, fires deferred completion if buffer dropped below threshold
//!
//! This matches WebRTC's C++ InternalSource::capture_frame + audio_task_ exactly:
//! - One buffer, one mutex
//! - Immediate vs deferred on_complete callback
//! - Only one deferred callback at a time

use std::collections::VecDeque;
use std::sync::Mutex;
use tracing::{debug, info};

/// Default queue_size_ms matching _ParticipantAudioOutput production usage (200ms).
/// rtc.AudioSource class default is 1000ms, but LiveKit voice agents override to 200ms
/// for tighter backpressure and faster interrupt response.
const DEFAULT_QUEUE_SIZE_MS: u32 = 200;

/// Completion callback — called from RTP send loop thread to signal Python.
/// This is the equivalent of WebRTC's on_complete(ctx) callback.
pub(crate) type CompletionCallback = Box<dyn FnOnce() + Send>;

/// Inner state protected by mutex (matches WebRTC's mutex_-guarded fields).
struct Inner {
    /// PCM samples buffer (matches WebRTC's buffer_).
    /// VecDeque for O(1) drain from front (WebRTC C++ uses deque-like circular buffer).
    pcm: VecDeque<i16>,
    /// Deferred completion callback (matches WebRTC's on_complete_ + capture_userdata_)
    /// Only one at a time — WebRTC rejects capture_frame if one is already pending.
    pending_complete: Option<CompletionCallback>,
    /// Flush flag — when set, pcm is cleared on next drain
    flush: bool,
}

/// Shared audio buffer for outbound audio.
/// Thread-safe: locked by both send_audio (Python thread) and RTP send loop (tokio thread).
pub(crate) struct AudioBuffer {
    inner: Mutex<Inner>,
    /// notify_threshold = queue_size_samples (matches WebRTC C++)
    notify_threshold: usize,
    /// capacity = 2 * queue_size_samples (matches WebRTC C++)
    capacity: usize,
    /// Sample rate used for debug logging
    sample_rate: u32,
}

impl AudioBuffer {
    /// Create with default queue_size_ms (200ms, matching _ParticipantAudioOutput production).
    pub fn new() -> Self {
        Self::with_queue_size(DEFAULT_QUEUE_SIZE_MS, 16000)
    }

    /// Create with configurable queue_size_ms and sample_rate
    /// (matches WebRTC C++ InternalSource constructor).
    /// - notify_threshold = queue_size_ms * sample_rate / 1000
    /// - capacity = 2 * notify_threshold
    pub fn with_queue_size(queue_size_ms: u32, sample_rate: u32) -> Self {
        let queue_size_samples = (queue_size_ms as u64 * sample_rate as u64 / 1000) as usize;
        let notify_threshold = queue_size_samples;
        let capacity = queue_size_samples + notify_threshold; // 2x, same as WebRTC C++
        info!(
            "AudioBuffer: queue_size_ms={} sample_rate={} threshold={} capacity={}",
            queue_size_ms, sample_rate, notify_threshold, capacity
        );
        Self {
            inner: Mutex::new(Inner {
                pcm: VecDeque::with_capacity(capacity),
                pending_complete: None,
                flush: false,
            }),
            notify_threshold,
            capacity,
            sample_rate,
        }
    }

    /// Push samples into the buffer (called from send_audio on Python thread).
    ///
    /// Matches WebRTC C++ InternalSource::capture_frame exactly:
    /// 1. Check capacity → reject if full
    /// 2. Append samples
    /// 3. If below threshold → fire on_complete immediately
    /// 4. If above threshold → store on_complete for deferred firing
    pub fn push(&self, samples: &[i16], on_complete: CompletionCallback) -> Result<(), &'static str> {
        let mut inner = self.inner.lock().unwrap();

        // Check capacity (matches WebRTC: available = capacity - buffer_.size())
        let available = self.capacity.saturating_sub(inner.pcm.len());
        if available < samples.len() {
            return Err("buffer full");
        }

        // Reject if a deferred callback is already pending
        // (matches WebRTC: if (on_complete_ || capture_userdata_) return false)
        if inner.pending_complete.is_some() {
            return Err("previous capture still pending");
        }

        // Append samples
        inner.pcm.extend(samples.iter().copied());

        // Decision: immediate vs deferred (matches WebRTC threshold check)
        let buf_len = inner.pcm.len();
        if buf_len <= self.notify_threshold {
            // Below threshold → fire immediately (matches WebRTC immediate on_complete)
            drop(inner);
            on_complete();
            Ok(())
        } else {
            // Above threshold → store for deferred firing by RTP loop
            debug!("AudioBuffer: deferred callback, buf={} samples ({}ms)", buf_len, buf_len * 1000 / self.sample_rate as usize);
            inner.pending_complete = Some(on_complete);
            Ok(())
        }
    }

    /// Drain up to `count` samples from the front of the buffer.
    /// Called by RTP send loop every 20ms.
    ///
    /// After draining, checks if a deferred completion should fire
    /// (matches WebRTC's audio_task_ firing on_complete_ when buffer <= threshold).
    pub fn drain(&self, count: usize) -> Vec<i16> {
        let mut inner = self.inner.lock().unwrap();

        // Check flush flag first
        if inner.flush {
            let flushed = inner.pcm.len();
            inner.pcm.clear();
            inner.flush = false;
            // Fire any pending completion immediately
            let cb = inner.pending_complete.take();
            drop(inner);
            if flushed > 0 {
                info!("AudioBuffer flush: cleared {} samples", flushed);
            }
            if let Some(cb) = cb { cb(); }
            return Vec::new();
        }

        let n = count.min(inner.pcm.len());
        let samples: Vec<i16> = if n > 0 {
            inner.pcm.drain(..n).collect()
        } else {
            Vec::new()
        };

        // Fire deferred completion if buffer dropped below threshold
        // (matches WebRTC: if on_complete_ && buffer_.size() <= notify_threshold_samples_)
        if inner.pending_complete.is_some() && inner.pcm.len() <= self.notify_threshold {
            let remaining = inner.pcm.len();
            let cb = inner.pending_complete.take();
            drop(inner);
            debug!("AudioBuffer: firing deferred callback, buf={} samples ({}ms)", remaining, remaining * 1000 / self.sample_rate as usize);
            if let Some(cb) = cb { cb(); }
        }

        samples
    }

    /// Get current buffer length in samples.
    pub fn len(&self) -> usize {
        self.inner.lock().unwrap().pcm.len()
    }

    /// Check if buffer is empty.
    pub fn is_empty(&self) -> bool {
        self.inner.lock().unwrap().pcm.is_empty()
    }

    /// Set flush flag — buffer will be cleared on next drain tick.
    pub fn set_flush(&self) {
        let mut inner = self.inner.lock().unwrap();
        inner.flush = true;
        // Also fire any pending completion immediately
        let cb = inner.pending_complete.take();
        drop(inner);
        if let Some(cb) = cb { cb(); }
    }

    /// Clear buffer immediately and fire pending completion.
    /// Called from clear_queue for immediate interrupt.
    pub fn clear(&self) {
        let mut inner = self.inner.lock().unwrap();
        let cleared = inner.pcm.len();
        inner.pcm.clear();
        inner.flush = false;
        let cb = inner.pending_complete.take();
        drop(inner);
        if cleared > 0 {
            info!("AudioBuffer clear: cleared {} samples", cleared);
        }
        if let Some(cb) = cb { cb(); }
    }
}
