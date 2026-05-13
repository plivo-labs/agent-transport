//! Shared audio buffer matching WebRTC C++ AudioSource's internal buffer.
//!
//! Architecture (post-0.2.0 — event-queue model, LiveKit-faithful):
//! - `send_audio` pushes samples under mutex, checks threshold
//! - **Every push always emits an `AudioCaptureComplete` event** —
//!   immediately if buffer is below threshold, deferred (via
//!   `pending_captures`) if above. Matches LiveKit's invariant that
//!   every `capture_audio_frame` request produces exactly one
//!   completion event (`livekit/rtc/audio_source.py:142-149`).
//! - `add_pending_playout` is symmetric — immediate emit if buffer is
//!   already empty, deferred otherwise.
//! - RTP send loop drains samples under same mutex every 20ms.
//! - After draining, emits `AudioCaptureComplete` for any async_id whose
//!   threshold condition is now met, and `AudioPlayoutComplete` for any
//!   playout-waiter whose buffer-empty condition is now met.
//!
//! **No `Box<dyn FnOnce() + Send>` callbacks are stored here.** Rust threads
//! never invoke Python application code — events flow through the
//! crossbeam channel to the central dispatch thread, which uses
//! `loop.call_soon_threadsafe` to hand off to Python's asyncio loop.
//! This eliminates the GIL+Mutex AB-BA deadlock that was hitting prod.

use std::collections::VecDeque;
use std::sync::Mutex;
use tracing::debug;

use crossbeam_channel::Sender;

use crate::events::EndpointEvent;
use crate::sync::LockExt;

/// Default queue_size_ms matching _ParticipantAudioOutput production usage (200ms).
/// rtc.AudioSource class default is 1000ms, but LiveKit voice agents override to 200ms
/// for tighter backpressure and faster interrupt response.
const DEFAULT_QUEUE_SIZE_MS: u32 = 200;

/// Inner state protected by mutex.
struct Inner {
    /// PCM samples buffer (matches WebRTC's buffer_).
    /// VecDeque for O(1) drain from front.
    pcm: VecDeque<i16>,
    /// async_ids of pushes awaiting "buffer dropped below threshold".
    /// VecDeque (FIFO) so pipelined captures resolve in submission order.
    pending_captures: VecDeque<u64>,
    /// Flush flag — when set, pcm is cleared on next drain
    flush: bool,
    /// async_ids of waiters awaiting "buffer drained to empty".
    /// Multiple concurrent waiters are supported — all resolve when buffer empties.
    pending_playouts: Vec<u64>,
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
    /// Session id used to tag every emitted event so adapters can route correctly.
    session_id: String,
    /// Channel for emitting audio-completion events. The receiver is the
    /// endpoint's event channel; events are picked up by `wait_for_event`
    /// (Python/Node) and dispatched through the asyncio loop.
    event_tx: Sender<EndpointEvent>,
}

impl AudioBuffer {
    /// Create with default queue_size_ms (200ms, matching _ParticipantAudioOutput production).
    pub fn new(session_id: String, event_tx: Sender<EndpointEvent>) -> Self {
        Self::with_queue_size(DEFAULT_QUEUE_SIZE_MS, 8000, session_id, event_tx)
    }

    /// Create with configurable queue_size_ms and sample_rate
    /// (matches WebRTC C++ InternalSource constructor).
    /// - notify_threshold = queue_size_ms * sample_rate / 1000
    /// - capacity = 2 * notify_threshold
    pub fn with_queue_size(
        queue_size_ms: u32,
        sample_rate: u32,
        session_id: String,
        event_tx: Sender<EndpointEvent>,
    ) -> Self {
        // Mirror LiveKit's `libwebrtc/src/native/audio_source.rs::NativeAudioSource::new`:
        // `let queue_size_samples = (queue_size_ms * sample_rate * num_channels) / 1000;`
        // We're mono-only today (num_channels = 1 implicitly), so the formula
        // collapses to the same value. If a future change adds stereo support,
        // this constructor MUST take num_channels and the multiplication MUST
        // be reintroduced — otherwise queue_size_samples would be 2x undersized
        // at stereo and LiveKit's 200 ms backpressure threshold would become
        // 100 ms.
        let queue_size_samples = (queue_size_ms as u64 * sample_rate as u64 / 1000) as usize;
        let notify_threshold = queue_size_samples;
        let capacity = queue_size_samples + notify_threshold; // 2x, same as WebRTC C++
        debug!(
            "AudioBuffer({}): queue_size_ms={} sample_rate={} threshold={} capacity={}",
            session_id, queue_size_ms, sample_rate, notify_threshold, capacity
        );
        Self {
            inner: Mutex::new(Inner {
                pcm: VecDeque::with_capacity(capacity),
                pending_captures: VecDeque::new(),
                flush: false,
                pending_playouts: Vec::new(),
            }),
            notify_threshold,
            capacity,
            sample_rate,
            session_id,
            event_tx,
        }
    }

    /// Push samples into the buffer (called from send_audio on Python/tokio thread).
    ///
    /// **Always emits exactly one `AudioCaptureComplete { async_id }`** —
    /// immediately if the buffer is at-or-below threshold after the push,
    /// or deferred until a subsequent `drain` brings the buffer back
    /// below threshold. This is the LiveKit invariant — every
    /// `capture_audio_frame` request produces exactly one completion
    /// event (`livekit/rtc/audio_source.py:142-149`).
    ///
    /// Returns `Err("buffer full")` synchronously if the push would
    /// overflow capacity — no event is emitted in that case (the caller
    /// gets a Python exception via the pyo3 binding and unwinds before
    /// reaching its `wait_for`).
    ///
    /// The caller is responsible for allocating `async_id` (typically
    /// from a monotonic counter on the endpoint) and ensuring it has
    /// `subscribe()`d to the endpoint's event broker BEFORE calling
    /// this — otherwise an immediate emit can land before the
    /// subscriber is ready.
    pub fn push(&self, samples: &[i16], async_id: u64) -> Result<(), &'static str> {
        let mut inner = self.inner.lock_or_recover();

        let available = self.capacity.saturating_sub(inner.pcm.len());
        if available < samples.len() {
            return Err("buffer full");
        }

        inner.pcm.extend(samples.iter().copied());

        let buf_len = inner.pcm.len();
        let emit_immediate = buf_len <= self.notify_threshold;
        if !emit_immediate {
            // Deferred — register async_id for drain-below-threshold notification
            debug!(
                "AudioBuffer({}): deferred async_id={}, buf={} samples ({}ms)",
                self.session_id,
                async_id,
                buf_len,
                buf_len * 1000 / self.sample_rate as usize
            );
            inner.pending_captures.push_back(async_id);
        }
        drop(inner);
        if emit_immediate {
            self.emit(EndpointEvent::AudioCaptureComplete {
                session_id: self.session_id.clone(),
                async_id,
            });
        }
        Ok(())
    }

    /// Register an async_id to be notified when the buffer drains to empty.
    ///
    /// **Always emits exactly one `AudioPlayoutComplete { async_id }`** —
    /// immediately if the buffer is already empty, deferred until a
    /// subsequent `drain` brings the buffer to empty. Matches LiveKit's
    /// "every request produces one completion event" invariant.
    ///
    /// Multiple concurrent waiters are supported — each gets its own
    /// async_id, and all of them resolve when the buffer next reaches
    /// empty.
    pub fn add_pending_playout(&self, async_id: u64) {
        let mut inner = self.inner.lock_or_recover();
        let emit_immediate = inner.pcm.is_empty();
        if !emit_immediate {
            inner.pending_playouts.push(async_id);
        }
        drop(inner);
        if emit_immediate {
            self.emit(EndpointEvent::AudioPlayoutComplete {
                session_id: self.session_id.clone(),
                async_id,
            });
        }
    }

    /// Drain up to `count` samples from the front of the buffer.
    /// Called by RTP send loop every 20ms.
    ///
    /// After draining, emits:
    /// - `AudioCaptureComplete` for every pending capture if buffer dropped
    ///   to-or-below threshold;
    /// - `AudioPlayoutComplete` for every pending playout if buffer emptied.
    ///
    /// On flush flag: clears the buffer, emits `AudioCaptureError` for every
    /// pending capture and pending playout with reason "flushed".
    pub fn drain(&self, count: usize) -> Vec<i16> {
        let mut inner = self.inner.lock_or_recover();

        // Flush path — clear buffer, cancel all pending operations
        if inner.flush {
            let flushed = inner.pcm.len();
            inner.pcm.clear();
            inner.flush = false;
            let captures: Vec<u64> = inner.pending_captures.drain(..).collect();
            let playouts: Vec<u64> = inner.pending_playouts.drain(..).collect();
            drop(inner);
            if flushed > 0 {
                debug!(
                    "AudioBuffer({}) flush: cleared {} samples",
                    self.session_id, flushed
                );
            }
            self.emit_errors(captures, "flushed");
            self.emit_errors(playouts, "flushed");
            return Vec::new();
        }

        let n = count.min(inner.pcm.len());
        let samples: Vec<i16> = if n > 0 {
            inner.pcm.drain(..n).collect()
        } else {
            Vec::new()
        };

        // Collect pending captures to fire if buffer dropped at-or-below threshold
        let captures_to_fire: Vec<u64> = if !inner.pending_captures.is_empty()
            && inner.pcm.len() <= self.notify_threshold
        {
            let remaining = inner.pcm.len();
            debug!(
                "AudioBuffer({}): firing {} deferred captures, buf={} samples ({}ms)",
                self.session_id,
                inner.pending_captures.len(),
                remaining,
                remaining * 1000 / self.sample_rate as usize
            );
            inner.pending_captures.drain(..).collect()
        } else {
            Vec::new()
        };

        // Collect pending playouts to fire if buffer is now empty
        let playouts_to_fire: Vec<u64> = if inner.pcm.is_empty() && !inner.pending_playouts.is_empty()
        {
            inner.pending_playouts.drain(..).collect()
        } else {
            Vec::new()
        };

        drop(inner);
        for async_id in captures_to_fire {
            self.emit(EndpointEvent::AudioCaptureComplete {
                session_id: self.session_id.clone(),
                async_id,
            });
        }
        for async_id in playouts_to_fire {
            self.emit(EndpointEvent::AudioPlayoutComplete {
                session_id: self.session_id.clone(),
                async_id,
            });
        }

        samples
    }

    /// Get current buffer length in samples.
    pub fn len(&self) -> usize {
        self.inner.lock_or_recover().pcm.len()
    }

    /// Check if buffer is empty.
    pub fn is_empty(&self) -> bool {
        self.inner.lock_or_recover().pcm.is_empty()
    }

    /// Set flush flag — buffer will be cleared on next drain tick.
    /// All pending captures and playouts are cancelled immediately
    /// with `AudioCaptureError { error: "flushed" }`.
    pub fn set_flush(&self) {
        let mut inner = self.inner.lock_or_recover();
        inner.flush = true;
        let captures: Vec<u64> = inner.pending_captures.drain(..).collect();
        let playouts: Vec<u64> = inner.pending_playouts.drain(..).collect();
        drop(inner);
        self.emit_errors(captures, "flushed");
        self.emit_errors(playouts, "flushed");
    }

    /// Push samples without backpressure — drop if buffer full.
    /// Used for background audio which is continuous and low priority.
    ///
    /// Does not allocate or fire any async_ids — background audio has no
    /// completion semantics (it's continuous).
    pub fn push_no_backpressure(&self, samples: &[i16]) {
        let mut inner = self.inner.lock_or_recover();
        let buf_len = inner.pcm.len();
        let available = self.capacity.saturating_sub(buf_len);
        if available >= samples.len() {
            inner.pcm.extend(samples.iter().copied());
        } else {
            let dropped = samples.len();
            let sr = self.sample_rate;
            drop(inner);
            debug!(
                "AudioBuffer({}): no-backpressure drop: dropped={} samples ({}ms), buf={} ({}ms), cap={} ({}ms)",
                self.session_id,
                dropped,
                dropped * 1000 / sr as usize,
                buf_len,
                buf_len * 1000 / sr as usize,
                self.capacity,
                self.capacity * 1000 / sr as usize,
            );
        }
    }

    /// Clear buffer immediately. All pending captures and playouts complete
    /// with **success** (frame discarded silently for captures; nothing to
    /// wait for, so playouts resolve).
    ///
    /// This matches LiveKit's
    /// `rtc.AudioSource.clear_queue` FFI semantics: the queue is wiped and
    /// pending requests resolve normally. The previous behaviour (emit
    /// `AudioCaptureError("cleared")`) caused LiveKit's base
    /// ``_ParticipantAudioOutput._forward_audio`` to die on the very first
    /// caller interrupt — its `await self._audio_source.capture_frame(frame)`
    /// raised `RuntimeError("cleared")`, propagated out of the `async for`
    /// loop, and the task ended uncaught. Subsequent TTS turns piled into
    /// `_audio_buf` with no consumer.
    ///
    /// True error paths (session torn down, encoder failed) still emit
    /// `AudioCaptureError` via other code paths — only the user-triggered
    /// "interruption" clear is reclassified as success here.
    pub fn clear(&self) {
        let mut inner = self.inner.lock_or_recover();
        let cleared = inner.pcm.len();
        inner.pcm.clear();
        inner.flush = false;
        let captures: Vec<u64> = inner.pending_captures.drain(..).collect();
        let playouts: Vec<u64> = inner.pending_playouts.drain(..).collect();
        drop(inner);
        if cleared > 0 {
            debug!(
                "AudioBuffer({}) clear: cleared {} samples",
                self.session_id, cleared
            );
        }
        for async_id in captures {
            self.emit(EndpointEvent::AudioCaptureComplete {
                session_id: self.session_id.clone(),
                async_id,
            });
        }
        for async_id in playouts {
            self.emit(EndpointEvent::AudioPlayoutComplete {
                session_id: self.session_id.clone(),
                async_id,
            });
        }
    }

    /// Get queued audio duration in milliseconds (real buffer state).
    /// Matches WebRTC's audioSource.queuedDuration.
    pub fn queued_duration_ms(&self, sample_rate: u32) -> f64 {
        let len = self.inner.lock_or_recover().pcm.len();
        (len as f64 / sample_rate as f64) * 1000.0
    }

    // ─── Internal helpers ──────────────────────────────────────────────────

    /// Emit one event. Silently drops if the receiver is gone (endpoint
    /// shutdown). MUST be called without holding `self.inner`.
    fn emit(&self, event: EndpointEvent) {
        let _ = self.event_tx.try_send(event);
    }

    /// Emit `AudioCaptureError` for every async_id in `ids`.
    fn emit_errors(&self, ids: Vec<u64>, reason: &str) {
        for async_id in ids {
            self.emit(EndpointEvent::AudioCaptureError {
                session_id: self.session_id.clone(),
                async_id,
                error: reason.into(),
            });
        }
    }
}

impl Drop for AudioBuffer {
    fn drop(&mut self) {
        // Emit `AudioCaptureError` for any still-pending operations so the
        // Python/Node awaiters don't hang forever on session teardown.
        //
        // CRITICAL: this is a Rust thread emitting protobuf-style events
        // through a crossbeam channel — there is no `Box<dyn FnOnce>` Python
        // closure being invoked here. The pre-0.2.0 deadlock was caused by
        // `cb()` (Python re-entry) happening while a sessions Mutex was held
        // by the caller. With the event-channel model that risk is structural-
        // ly impossible: events never run Python code on Rust threads.
        let mut inner = self.inner.lock_or_recover();
        let captures: Vec<u64> = inner.pending_captures.drain(..).collect();
        let playouts: Vec<u64> = inner.pending_playouts.drain(..).collect();
        drop(inner);
        for async_id in captures {
            let _ = self.event_tx.try_send(EndpointEvent::AudioCaptureError {
                session_id: self.session_id.clone(),
                async_id,
                error: "buffer_dropped".into(),
            });
        }
        for async_id in playouts {
            let _ = self.event_tx.try_send(EndpointEvent::AudioCaptureError {
                session_id: self.session_id.clone(),
                async_id,
                error: "buffer_dropped".into(),
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossbeam_channel::Receiver;

    /// Build a fresh buffer + a receiver for inspecting events.
    /// 200ms @ 8kHz → threshold 1600, capacity 3200.
    fn new_buf() -> (AudioBuffer, Receiver<EndpointEvent>) {
        let (tx, rx) = crossbeam_channel::unbounded();
        let buf = AudioBuffer::with_queue_size(200, 8000, "test-session".into(), tx);
        (buf, rx)
    }

    /// Drain the receiver and return all queued events. Non-blocking.
    fn drain_events(rx: &Receiver<EndpointEvent>) -> Vec<EndpointEvent> {
        let mut events = Vec::new();
        while let Ok(e) = rx.try_recv() {
            events.push(e);
        }
        events
    }

    fn capture_complete_ids(events: &[EndpointEvent]) -> Vec<u64> {
        events
            .iter()
            .filter_map(|e| match e {
                EndpointEvent::AudioCaptureComplete { async_id, .. } => Some(*async_id),
                _ => None,
            })
            .collect()
    }

    fn playout_complete_ids(events: &[EndpointEvent]) -> Vec<u64> {
        events
            .iter()
            .filter_map(|e| match e {
                EndpointEvent::AudioPlayoutComplete { async_id, .. } => Some(*async_id),
                _ => None,
            })
            .collect()
    }

    fn capture_error_ids<'a>(events: &'a [EndpointEvent]) -> Vec<(u64, &'a str)> {
        events
            .iter()
            .filter_map(|e| match e {
                EndpointEvent::AudioCaptureError {
                    async_id, error, ..
                } => Some((*async_id, error.as_str())),
                _ => None,
            })
            .collect()
    }

    // ─── push / drain basics ─────────────────────────────────────────────

    #[test]
    fn test_push_below_threshold_emits_immediately() {
        let (buf, rx) = new_buf();
        buf.push(&vec![0i16; 100], 1).unwrap();
        let events = drain_events(&rx);
        assert_eq!(
            capture_complete_ids(&events),
            vec![1],
            "below threshold → immediate AudioCaptureComplete (LiveKit invariant: \
             every push produces exactly one completion event)"
        );
    }

    #[test]
    fn test_push_above_threshold_defers_and_fires_on_drain() {
        let (buf, rx) = new_buf();
        // 2000 > threshold 1600 → defer
        buf.push(&vec![0i16; 2000], 42).unwrap();
        assert!(drain_events(&rx).is_empty(), "no event yet — buffer still full");

        // Drain enough to drop below threshold
        let _ = buf.drain(500);
        let events = drain_events(&rx);
        assert_eq!(
            capture_complete_ids(&events),
            vec![42],
            "drain emits AudioCaptureComplete for async_id 42"
        );
    }

    #[test]
    fn test_push_always_emits_exactly_one_event_per_async_id() {
        // LiveKit invariant: every push() either emits immediately (below
        // threshold) or queues for deferred emission — never both, never neither.
        // This test verifies the invariant by running a mix of small and
        // large pushes and counting completion events.
        let (buf, rx) = new_buf();
        buf.push(&vec![0i16; 100], 1).unwrap();   // below → immediate
        buf.push(&vec![0i16; 2000], 2).unwrap();  // above → deferred
        buf.push(&vec![0i16; 100], 3).unwrap();   // pushed above-threshold (buf already 2100) → deferred
        let events_before_drain = drain_events(&rx);
        let mut completed_immediately = capture_complete_ids(&events_before_drain);
        completed_immediately.sort();
        assert_eq!(completed_immediately, vec![1], "only id 1 fires immediately");

        // Drain enough to clear below threshold
        let _ = buf.drain(1500);
        let mut all_completed = capture_complete_ids(&drain_events(&rx));
        all_completed.sort();
        assert_eq!(all_completed, vec![2, 3], "ids 2 and 3 fire on drain");
    }

    #[test]
    fn test_push_rejects_when_full() {
        let (buf, _rx) = new_buf();
        let _ = buf.push(&vec![0i16; 3200], 1);
        let r = buf.push(&vec![0i16; 100], 2);
        assert!(r.is_err(), "buffer full should reject");
    }

    #[test]
    fn test_multiple_pending_captures_all_fire_on_drain() {
        // Unlike pre-0.2.0 (only one pending allowed at a time), the new
        // event-based model supports multiple pipelined deferred captures.
        let (buf, rx) = new_buf();
        // First push: 2000 > threshold → defer async_id 1
        buf.push(&vec![0i16; 2000], 1).unwrap();
        // Second push: buffer at 2000, can fit 1200 more (capacity 3200).
        // Push 1000 → total 3000. Above threshold → defer async_id 2.
        buf.push(&vec![0i16; 1000], 2).unwrap();
        assert!(drain_events(&rx).is_empty());

        // Drain enough to drop below threshold (3000 → 1500 after drain 1500)
        let _ = buf.drain(1500);
        let events = drain_events(&rx);
        let ids = capture_complete_ids(&events);
        assert_eq!(ids, vec![1, 2], "both pending captures fire in FIFO order");
    }

    #[test]
    fn test_drain_returns_samples_in_order() {
        let (buf, _rx) = new_buf();
        let input: Vec<i16> = (0..500).map(|i| i as i16).collect();
        buf.push(&input, 1).unwrap();
        let drained = buf.drain(500);
        assert_eq!(drained, input);
        assert!(buf.is_empty());
    }

    #[test]
    fn test_drain_empty_returns_empty() {
        let (buf, _rx) = new_buf();
        let d = buf.drain(100);
        assert!(d.is_empty());
    }

    #[test]
    fn test_len_and_is_empty() {
        let (buf, _rx) = new_buf();
        assert!(buf.is_empty());
        assert_eq!(buf.len(), 0);
        buf.push(&vec![0i16; 100], 1).unwrap();
        assert_eq!(buf.len(), 100);
        assert!(!buf.is_empty());
    }

    // ─── pending_playout semantics ───────────────────────────────────────

    #[test]
    fn test_add_pending_playout_on_empty_buffer_emits_immediately() {
        let (buf, rx) = new_buf();
        buf.add_pending_playout(99);
        let events = drain_events(&rx);
        assert_eq!(
            playout_complete_ids(&events),
            vec![99],
            "AudioPlayoutComplete fired immediately on empty buffer"
        );
    }

    #[test]
    fn test_pending_playout_fires_on_drain_to_empty() {
        let (buf, rx) = new_buf();
        buf.push(&vec![0i16; 100], 1).unwrap();
        // push of 100 (below threshold) emits AudioCaptureComplete immediately
        let pre_playout = drain_events(&rx);
        assert_eq!(capture_complete_ids(&pre_playout), vec![1], "capture id 1 fires immediately");

        buf.add_pending_playout(7);
        let pre_drain = drain_events(&rx);
        assert!(playout_complete_ids(&pre_drain).is_empty(), "playout 7 not yet fired");

        let _ = buf.drain(100);
        let post_drain = drain_events(&rx);
        assert_eq!(
            playout_complete_ids(&post_drain),
            vec![7],
            "drain-to-empty fires playout complete"
        );
    }

    #[test]
    fn test_pending_playout_not_fired_while_samples_remain() {
        let (buf, rx) = new_buf();
        buf.push(&vec![0i16; 500], 1).unwrap();
        buf.add_pending_playout(7);
        let _ = buf.drain(200);
        assert!(playout_complete_ids(&drain_events(&rx)).is_empty());
        let _ = buf.drain(200);
        assert!(playout_complete_ids(&drain_events(&rx)).is_empty());
        let _ = buf.drain(200); // now empty
        assert_eq!(playout_complete_ids(&drain_events(&rx)), vec![7]);
    }

    #[test]
    fn test_multiple_concurrent_playouts_all_fire() {
        // Unlike pre-0.2.0 (one playout callback, replace-fires-previous),
        // the new model supports multiple concurrent waiters cleanly.
        let (buf, rx) = new_buf();
        buf.push(&vec![0i16; 500], 1).unwrap();
        buf.add_pending_playout(10);
        buf.add_pending_playout(20);
        buf.add_pending_playout(30);
        assert!(playout_complete_ids(&drain_events(&rx)).is_empty());
        let _ = buf.drain(500);
        let events = drain_events(&rx);
        let mut ids = playout_complete_ids(&events);
        ids.sort();
        assert_eq!(ids, vec![10, 20, 30]);
    }

    // ─── Drop behavior ───────────────────────────────────────────────────

    #[test]
    fn test_drop_emits_capture_error_for_pending() {
        // Pre-0.2.0 deadlock root cause: Drop fired Python callbacks while
        // the sessions Mutex was held by the dropper. Now: Drop emits events
        // through a crossbeam channel — Python code never runs here.
        let (tx, rx) = crossbeam_channel::unbounded();
        {
            let buf = AudioBuffer::with_queue_size(200, 8000, "sess".into(), tx);
            buf.push(&vec![0i16; 2000], 42).unwrap();
            buf.add_pending_playout(7);
            // drop happens here at end of scope
        }
        let events = drain_events(&rx);
        let errors = capture_error_ids(&events);
        // Both pending capture and pending playout get terminal errors
        let ids: Vec<u64> = errors.iter().map(|(id, _)| *id).collect();
        let mut sorted = ids.clone();
        sorted.sort();
        assert_eq!(sorted, vec![7, 42]);
        for (_, reason) in errors {
            assert_eq!(reason, "buffer_dropped");
        }
    }

    // ─── clear semantics ─────────────────────────────────────────────────

    #[test]
    fn test_clear_emits_success_for_both() {
        // Pinned to LiveKit's `rtc.AudioSource.clear_queue` FFI semantics:
        // pending captures complete with success (frame discarded silently),
        // pending playouts complete with success. NOT an error — caller's
        // `await audio_source.capture_frame(frame)` should NOT raise on a
        // user-triggered clear, otherwise LiveKit's base
        // ``_ParticipantAudioOutput._forward_audio`` task dies on the first
        // interruption.
        let (buf, rx) = new_buf();
        buf.push(&vec![0i16; 2000], 1).unwrap();
        buf.add_pending_playout(2);
        let _ = drain_events(&rx);

        buf.clear();
        assert!(buf.is_empty());
        let events = drain_events(&rx);

        // No errors should be emitted.
        let errors = capture_error_ids(&events);
        assert!(errors.is_empty(), "clear must not emit AudioCaptureError; got {:?}", errors);

        // Both async_ids must receive completion events.
        let mut completes: Vec<u64> = events.iter().filter_map(|e| match e {
            EndpointEvent::AudioCaptureComplete { async_id, .. } => Some(*async_id),
            EndpointEvent::AudioPlayoutComplete { async_id, .. } => Some(*async_id),
            _ => None,
        }).collect();
        completes.sort();
        assert_eq!(completes, vec![1, 2]);
    }

    // ─── flush semantics ─────────────────────────────────────────────────

    #[test]
    fn test_set_flush_emits_capture_error_for_pending() {
        let (buf, rx) = new_buf();
        buf.push(&vec![0i16; 2000], 1).unwrap();
        buf.add_pending_playout(2);
        let _ = drain_events(&rx);

        buf.set_flush();
        let events = drain_events(&rx);
        let errors = capture_error_ids(&events);
        let mut ids: Vec<u64> = errors.iter().map(|(id, _)| *id).collect();
        ids.sort();
        assert_eq!(ids, vec![1, 2]);
        for (_, reason) in errors {
            assert_eq!(reason, "flushed");
        }
    }

    #[test]
    fn test_drain_after_flush_clears_buffer() {
        let (buf, _rx) = new_buf();
        buf.push(&vec![0i16; 1000], 1).unwrap();
        buf.set_flush();
        let drained = buf.drain(100);
        assert!(drained.is_empty(), "flush returns empty on drain");
        assert!(buf.is_empty());
    }

    // ─── push_no_backpressure (background audio) ─────────────────────────

    #[test]
    fn test_push_no_backpressure_drops_silently_when_full() {
        let (buf, _rx) = new_buf();
        buf.push_no_backpressure(&vec![0i16; 3200]);
        assert_eq!(buf.len(), 3200);
        buf.push_no_backpressure(&vec![0i16; 100]);
        assert_eq!(buf.len(), 3200, "push_no_backpressure drops when full");
    }

    #[test]
    fn test_push_no_backpressure_emits_no_events() {
        let (buf, rx) = new_buf();
        buf.push_no_backpressure(&vec![0i16; 500]);
        assert_eq!(buf.len(), 500);
        assert!(
            drain_events(&rx).is_empty(),
            "background audio never emits backpressure events"
        );
    }

    // ─── queued_duration_ms ──────────────────────────────────────────────

    #[test]
    fn test_queued_duration_ms() {
        let (buf, _rx) = new_buf();
        buf.push(&vec![0i16; 800], 1).unwrap();
        let dur = buf.queued_duration_ms(8000);
        assert!((dur - 100.0).abs() < 0.01, "expected ~100ms, got {}", dur);
    }

    // ─── single-fire invariants ──────────────────────────────────────────

    #[test]
    fn test_capture_event_fires_once_per_async_id() {
        let (buf, rx) = new_buf();
        buf.push(&vec![0i16; 2000], 42).unwrap();
        let _ = buf.drain(500); // drops below threshold
        assert_eq!(capture_complete_ids(&drain_events(&rx)), vec![42]);
        let _ = buf.drain(500);
        let _ = buf.drain(500);
        assert!(
            capture_complete_ids(&drain_events(&rx)).is_empty(),
            "no refire on subsequent drains"
        );
    }

    #[test]
    fn test_playout_event_fires_once_per_async_id() {
        let (buf, rx) = new_buf();
        buf.push(&vec![0i16; 500], 1).unwrap();
        buf.add_pending_playout(7);
        let _ = buf.drain(500);
        assert_eq!(playout_complete_ids(&drain_events(&rx)), vec![7]);
        let _ = buf.drain(100);
        let _ = buf.drain(100);
        assert!(playout_complete_ids(&drain_events(&rx)).is_empty());
    }

    // ─── REGRESSION: the deadlock guarantee ──────────────────────────────

    #[test]
    fn test_drop_never_invokes_user_code() {
        // This is the architectural guarantee — Drop emits events on a
        // crossbeam channel and returns. No `Box<dyn FnOnce>` is invoked.
        // If this test compiles and runs without locking up, the deadlock
        // class is structurally closed.
        let (tx, rx) = crossbeam_channel::unbounded();
        {
            let buf = AudioBuffer::with_queue_size(200, 8000, "s".into(), tx);
            // Stage maximal in-flight state
            buf.push(&vec![0i16; 2000], 1).unwrap();
            buf.push(&vec![0i16; 1000], 2).unwrap();
            buf.add_pending_playout(3);
            buf.add_pending_playout(4);
        }
        let events = drain_events(&rx);
        assert_eq!(events.len(), 4, "all 4 pending async_ids get terminal events");
    }
}
