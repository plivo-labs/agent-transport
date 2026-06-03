//! Generic WebSocket audio streaming endpoint.
//!
//! Provider-agnostic: all protocol specifics (message parsing, audio encoding,
//! hangup API) are delegated to a `StreamProtocol` implementation.
//!
//! This module handles: WebSocket server, session management, audio mixing,
//! resampling (speexdsp), checkpoint-paced send loop, recording, beep detection.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use crossbeam_channel::{Receiver, Sender};
use tokio::net::TcpListener;
use tokio::runtime::Runtime;
use tokio_tungstenite::tungstenite::Message;
use tokio_util::sync::CancellationToken;
use tracing::{debug, error, info, warn};

use beep_detector::{BeepDetector, BeepDetectorConfig, BeepDetectorResult};
use crate::audio::AudioFrame;
use crate::error::{EndpointError, Result};
use crate::events::EndpointEvent;
use crate::sip::audio_buffer::AudioBuffer;
use crate::sync::LockExt;
use super::config::AudioStreamConfig;
use super::protocol::{StreamEvent, StreamProtocol, WireEncoding};

// ─── Session context ─────────────────────────────────────────────────────────

struct StreamSession {
    call_id: String,
    stream_id: String,
    ws_tx: tokio::sync::mpsc::UnboundedSender<Message>,
    incoming_tx: Sender<AudioFrame>,
    incoming_rx: Receiver<AudioFrame>,
    audio_buf: Arc<AudioBuffer>,
    bg_audio_buf: Arc<AudioBuffer>,
    /// Two-state lifecycle: `false` = Active (transport up), `true` = Terminated
    /// (transport gone, but the session struct lingers in the HashMap until
    /// Python calls `hangup(sid)`). After `terminated` is true, audio FFI
    /// methods (`send_audio_async`, `wait_for_playout_async`,
    /// `clear_buffer`, `pause`, `resume`) return success-early with
    /// no-op semantics, mirroring LiveKit's `rtc.AudioSource` behaviour
    /// after `_ffi_handle.disposed`. This lets the Python audio source
    /// outlive the transport without surfacing `CallNotActive` from the
    /// race window between WS close and Python `aclose()`.
    terminated: Arc<AtomicBool>,
    /// Resampler for incoming agent audio (e.g. 24kHz TTS → 8kHz wire rate).
    /// Lazy-initialized on first mismatched frame, matching SIP endpoint pattern.
    input_resampler: Arc<Mutex<Option<crate::sip::resampler::Resampler>>>,
    extra_headers: HashMap<String, String>,
    encoding: WireEncoding,
    muted: Arc<AtomicBool>,
    paused: Arc<AtomicBool>,
    checkpoint_counter: AtomicU64,
    checkpoint_notify: Arc<(Mutex<Option<String>>, Condvar)>,
    send_loop_notify: Arc<tokio::sync::Notify>,
    /// Checkpoint name queued by flush() — send loop sends it when buffer empties.
    /// Ensures checkpoint is ordered AFTER all playAudio messages.
    pending_flush: Arc<Mutex<Option<String>>>,
    /// The checkpoint name we're currently waiting for Plivo to confirm.
    /// Set when the send loop sends the checkpoint, cleared on confirm or clear_buffer.
    /// CheckpointAck only notifies condvar if the name matches, preventing stale acks.
    awaiting_checkpoint: Arc<Mutex<Option<String>>>,
    recorder: Arc<Mutex<Option<Arc<crate::recorder::CallRecorder>>>>,
    beep_detector: Arc<Mutex<Option<BeepDetector>>>,
    cancel: CancellationToken,
}

// ─── AudioStreamEndpoint ─────────────────────────────────────────────────────

pub struct AudioStreamEndpoint {
    config: AudioStreamConfig,
    protocol: Arc<dyn StreamProtocol>,
    runtime: Runtime,
    sessions: Arc<Mutex<HashMap<String, StreamSession>>>,
    event_tx: Sender<EndpointEvent>,
    event_rx: Receiver<EndpointEvent>,
    cancel: CancellationToken,
    recording_mgr: Arc<crate::recorder::RecordingManager>,
    /// Monotonic counter for allocating async_ids returned by
    /// `send_audio_async` / `wait_for_playout_async`. Adapters use these
    /// ids to await the matching `EndpointEvent` (AudioCaptureComplete,
    /// AudioPlayoutComplete, AudioCaptureError) from the event channel.
    async_id_counter: Arc<AtomicU64>,
}

impl AudioStreamEndpoint {
    pub fn new(config: AudioStreamConfig, protocol: Arc<dyn StreamProtocol>) -> Result<Self> {
        if config.input_sample_rate == 0 || config.output_sample_rate == 0 { return Err(EndpointError::Other("sample_rate must be > 0".into())); }
        let rt = Runtime::new().map_err(|e| EndpointError::Other(e.to_string()))?;
        // Bounded so a stalled Python dispatcher can't grow this without limit
        // (OOM). Emits use try_send → drop-on-full; the dispatcher warns at a
        // high-water mark well before the cap. See events::EVENT_CHANNEL_CAP.
        let (etx, erx) = crossbeam_channel::bounded(crate::events::EVENT_CHANNEL_CAP);
        let cancel = CancellationToken::new();
        let sessions = Arc::new(Mutex::new(HashMap::new()));

        let recording_mgr = crate::recorder::RecordingManager::new();

        let (addr, sess, etx2, cc, isr, osr, proto, rmgr) = (
            config.listen_addr.clone(), sessions.clone(), etx.clone(),
            cancel.clone(), config.input_sample_rate, config.output_sample_rate, protocol.clone(),
            recording_mgr.clone(),
        );
        rt.spawn(async move {
            if let Err(e) = run_ws_server(&addr, sess, etx2, cc, isr, osr, proto, rmgr).await {
                error!("WS server: {}", e);
            }
        });

        info!("Audio streaming endpoint on ws://{}", config.listen_addr);
        Ok(Self {
            config, protocol, runtime: rt, sessions, event_tx: etx, event_rx: erx,
            cancel, recording_mgr,
            async_id_counter: Arc::new(AtomicU64::new(1)),
        })
    }

    // ─── Audio send/recv ─────────────────────────────────────────────────

    /// Push audio frame and return the `async_id` the caller must await
    /// on `EndpointEvent::AudioCaptureComplete` (or `AudioCaptureError`).
    ///
    /// **Always returns `Ok(async_id)` on success** — the completion
    /// event always fires (immediately if buffer ended at-or-below
    /// threshold, deferred if above). Mirrors LiveKit's
    /// `capture_audio_frame` invariant: every request produces exactly
    /// one matching event (`livekit/rtc/audio_source.py:142-149`).
    ///
    /// Returns `Err` synchronously only for misuse (session not active,
    /// buffer overflow). In the error case no event is emitted, so the
    /// caller's `wait_for(predicate)` would not find a match — callers
    /// MUST unsubscribe on the synchronous-error path before awaiting.
    pub fn send_audio_async(&self, session_id: &str, frame: &AudioFrame) -> Result<u64> {
        let async_id = self.async_id_counter.fetch_add(1, Ordering::Relaxed);
        let (audio_buf, resampler) = {
            let s = self.sessions.lock_or_recover();
            let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
            // Terminated session: short-circuit with immediate completion.
            // Matches LiveKit's `rtc.AudioSource.capture_frame:119` early-return
            // on `_ffi_handle.disposed` — the equivalent pre-call guard for our
            // sid-keyed handle model. Audio is discarded; the per-frame async_id
            // contract is preserved so the Python audio source's wait_for(...)
            // resolves without surfacing CallNotActive.
            if sess.terminated.load(Ordering::Acquire) {
                let _ = self.event_tx.try_send(EndpointEvent::AudioCaptureComplete {
                    session_id: session_id.to_string(),
                    async_id,
                    cancelled: false,
                });
                return Ok(async_id);
            }
            (sess.audio_buf.clone(), sess.input_resampler.clone())
        };
        let target_rate = self.config.output_sample_rate;
        let result = if frame.sample_rate != 0 && frame.sample_rate != target_rate {
            // Resample incoming audio (e.g. 24kHz TTS → 8kHz wire rate).
            // Recreate the resampler if the source sample rate has changed
            // (e.g. TTS switched from 24kHz to 16kHz) — otherwise speex filter
            // state from the old rate produces corrupted output.
            let mut guard = resampler.lock_or_recover();
            let needs_new = match guard.as_ref() {
                None => true,
                Some(r) => r.from_rate() != frame.sample_rate,
            };
            if needs_new {
                info!("send_audio: resampling {}Hz -> {}Hz", frame.sample_rate, target_rate);
                *guard = crate::sip::resampler::Resampler::new_voip(frame.sample_rate, target_rate);
            }
            if let Some(ref mut r) = *guard {
                let resampled = r.process(&frame.data).to_vec();
                audio_buf.push(&resampled, async_id)
            } else {
                audio_buf.push(&frame.data, async_id)
            }
        } else {
            audio_buf.push(&frame.data, async_id)
        };
        result.map_err(|e| EndpointError::Other(e.into()))?;
        Ok(async_id)
    }

    /// Convenience: push audio without awaiting the completion event.
    /// Internally still produces an `AudioCaptureComplete` event — the
    /// caller just doesn't consume it. Useful for adapters that don't
    /// need backpressure semantics. Note that a subscriber subscribed
    /// with no filter will still receive these events.
    pub fn send_audio(&self, session_id: &str, frame: &AudioFrame) -> Result<()> {
        let _ = self.send_audio_async(session_id, frame)?;
        Ok(())
    }

    pub fn send_background_audio(&self, session_id: &str, frame: &AudioFrame) -> Result<()> {
        let bg_buf = {
            let s = self.sessions.lock_or_recover();
            let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
            // Terminated session: silently drop. Mirrors LiveKit's behaviour
            // where a published track gets auto-unpublished on disconnect and
            // the producer's loop stops receiving feedback. Without this short
            // circuit, the `_forward_track_audio` task in TransportRoom keeps
            // pumping frames into bg_audio_buf for the ~hundreds of ms between
            // WS-close and Python `_on_session_ended` cancellation, and the
            // buffer fills then drops at ~10ms cadence (visible as 100+
            // "no-backpressure drop" log lines on every call teardown).
            if sess.terminated.load(Ordering::Acquire) {
                return Ok(());
            }
            sess.bg_audio_buf.clone()
        };
        bg_buf.push_no_backpressure(&frame.data);
        Ok(())
    }

    pub fn recv_audio(&self, session_id: &str) -> Result<Option<AudioFrame>> {
        let s = self.sessions.lock_or_recover();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        Ok(sess.incoming_rx.try_recv().ok())
    }

    pub fn recv_audio_blocking(&self, session_id: &str, timeout_ms: u64) -> Result<Option<AudioFrame>> {
        let rx = {
            let s = self.sessions.lock_or_recover();
            s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?.incoming_rx.clone()
        };
        Ok(rx.recv_timeout(std::time::Duration::from_millis(timeout_ms)).ok())
    }

    // ─── Playback control ────────────────────────────────────────────────

    pub fn mute(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock_or_recover();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        sess.muted.store(true, Ordering::Release); Ok(())
    }

    pub fn unmute(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock_or_recover();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        sess.muted.store(false, Ordering::Release); Ok(())
    }

    pub fn pause(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock_or_recover();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        // Terminated session: no-op (no transport left to pause).
        if sess.terminated.load(Ordering::Acquire) {
            return Ok(());
        }
        sess.paused.store(true, Ordering::Release);
        // Send clearAudio to immediately stop playback on Plivo's side.
        // Plivo doesn't support muteStream — clearAudio is the only way to stop playback.
        // The Rust AudioBuffer retains queued audio for resume (send loop skips drain while paused).
        let json = self.protocol.build_clear_audio(&sess.stream_id);
        let _ = sess.ws_tx.send(Message::Text(json));
        debug!("Paused session {} (clearAudio sent to provider)", session_id);
        Ok(())
    }

    pub fn resume(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock_or_recover();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        // Terminated session: no-op.
        if sess.terminated.load(Ordering::Acquire) {
            return Ok(());
        }
        sess.paused.store(false, Ordering::Release);
        // No unmute needed — send loop will resume sending audio from the Rust buffer.
        debug!("Resumed session {}", session_id);
        Ok(())
    }

    // ─── Buffer / checkpoint / flush ─────────────────────────────────────

    /// Clear buffered audio — drains local AudioBuffer AND sends clear command to provider.
    /// Any audio already in the WS send queue will be overridden by the provider's clear.
    pub fn clear_buffer(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock_or_recover();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        // Terminated session: cleanup_session already cleared the buffer.
        // This call is a no-op (matches the LiveKit behaviour where
        // `clear_queue` on a disposed source is harmless).
        if sess.terminated.load(Ordering::Acquire) {
            return Ok(());
        }
        sess.audio_buf.clear();
        // Reset resampler to prevent stale filter artifacts on next speech
        *sess.input_resampler.lock_or_recover() = None;
        // Cancel any pending flush checkpoint — interrupt overrides playout
        *sess.pending_flush.lock_or_recover() = None;
        // Clear awaiting_checkpoint so late Plivo confirmations are ignored
        *sess.awaiting_checkpoint.lock_or_recover() = None;
        // Wake any blocked wait_for_playout so executor thread returns immediately
        {
            let (lock, cvar) = &*sess.checkpoint_notify;
            *lock.lock_or_recover() = Some("_cleared".into());
            cvar.notify_all();
        }
        // Only send clearAudio if not already paused (pause already sent clearAudio)
        if !sess.paused.load(Ordering::Acquire) {
            let json = self.protocol.build_clear_audio(&sess.stream_id);
            sess.ws_tx.send(Message::Text(json)).map_err(|_| EndpointError::Other("WS send failed".into()))?;
        }
        Ok(())
    }

    pub fn checkpoint(&self, session_id: &str, name: Option<&str>) -> Result<String> {
        let s = self.sessions.lock_or_recover();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        let cp_name = name.map(String::from).unwrap_or_else(|| {
            format!("cp-{}", sess.checkpoint_counter.fetch_add(1, Ordering::Relaxed))
        });
        let json = self.protocol.build_checkpoint(&sess.stream_id, &cp_name);
        sess.ws_tx.send(Message::Text(json)).map_err(|_| EndpointError::Other("WS send failed".into()))?;
        debug!("Checkpoint '{}' sent for session {}", cp_name, session_id);
        Ok(cp_name)
    }

    /// Queue a flush checkpoint — the send loop sends it after all buffered audio.
    /// This ensures the checkpoint is ordered AFTER all playAudio messages,
    /// so Plivo's playedStream confirms when ALL audio has actually been played.
    pub fn flush(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock_or_recover();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        let cp_name = format!("cp-{}", sess.checkpoint_counter.fetch_add(1, Ordering::Relaxed));
        *sess.pending_flush.lock_or_recover() = Some(cp_name.clone());
        debug!("Flush: checkpoint '{}' queued for session {} (send loop will send after drain)", cp_name, session_id);
        Ok(())
    }

    pub fn wait_for_playout(&self, session_id: &str, timeout_ms: u64) -> Result<bool> {
        let notify = {
            let s = self.sessions.lock_or_recover();
            let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
            sess.checkpoint_notify.clone()
        };
        let (lock, cvar) = &*notify;
        let guard = lock.lock_or_recover();
        // wait_timeout_while returns PoisonError if the mutex becomes poisoned
        // mid-wait — recover the guard the same way lock_or_recover does so one
        // panicking task can't hang every subsequent wait_for_playout caller.
        let (mut guard, timeout) = match cvar.wait_timeout_while(
            guard,
            std::time::Duration::from_millis(timeout_ms),
            |cp| cp.is_none(),
        ) {
            Ok(r) => r,
            Err(e) => {
                warn!("wait_for_playout: checkpoint_notify poisoned, recovering");
                e.into_inner()
            }
        };
        *guard = None;
        Ok(!timeout.timed_out())
    }

    // ─── DTMF ────────────────────────────────────────────────────────────

    pub fn send_dtmf(&self, session_id: &str, digits: &str) -> Result<()> {
        let s = self.sessions.lock_or_recover();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        let json = self.protocol.build_send_dtmf(digits);
        sess.ws_tx.send(Message::Text(json)).map_err(|_| EndpointError::Other("WS send failed".into()))?;
        info!("DTMF '{}' sent on session {}", digits, session_id);
        Ok(())
    }

    // ─── Recording ───────────────────────────────────────────────────────

    pub fn start_recording(&self, session_id: &str, path: &str, stereo: bool) -> Result<()> {
        let mode = if stereo { crate::recorder::RecordingMode::Stereo } else { crate::recorder::RecordingMode::Mono };
        let sample_rate = self.config.output_sample_rate;
        let rec = self.recording_mgr.start(session_id, path, mode, sample_rate);
        let s = self.sessions.lock_or_recover();
        if let Some(sess) = s.get(session_id) {
            *sess.recorder.lock_or_recover() = Some(rec);
        }
        Ok(())
    }

    pub fn stop_recording(&self, session_id: &str) -> Result<()> {
        self.recording_mgr.stop(session_id);
        if let Some(sess) = self.sessions.lock_or_recover().get(session_id) {
            *sess.recorder.lock_or_recover() = None;
        }
        Ok(())
    }

    // ─── Beep detection ──────────────────────────────────────────────────

    pub fn detect_beep(&self, session_id: &str, config: BeepDetectorConfig) -> Result<()> {
        let s = self.sessions.lock_or_recover();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        *sess.beep_detector.lock_or_recover() = Some(BeepDetector::new(config));
        Ok(())
    }

    pub fn cancel_beep_detection(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock_or_recover();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        *sess.beep_detector.lock_or_recover() = None;
        Ok(())
    }

    // ─── Call control ────────────────────────────────────────────────────

    pub fn hangup(&self, session_id: &str) -> Result<()> {
        self.hangup_with_auth(session_id, None, None)
    }

    pub fn hangup_with_auth(&self, session_id: &str, auth_id: Option<&str>, auth_token: Option<&str>) -> Result<()> {
        // First transition: if still Active, terminate + cleanup + emit
        // CallTerminated. Idempotent — no-op if already terminated by the
        // WS-close path.
        terminate_and_cleanup(
            session_id, &self.sessions, &self.recording_mgr, &self.event_tx,
            "hangup".into(),
        );
        // Second transition: remove from HashMap. This is the "release the
        // FFI handle" step — equivalent to LiveKit's `_ffi_handle.dispose()`.
        let call_id = match self.sessions.lock_or_recover().remove(session_id) {
            Some(s) => {
                // Send a WebSocket Close frame so Plivo tears down the call
                // cleanly. If the WS is already gone (transport-initiated
                // termination), the send is a silent no-op.
                info!("hangup: sending WS Close frame for session {}", session_id);
                let _ = s.ws_tx.send(Message::Close(None));
                s.call_id.clone()
            }
            None => return Ok(())
        };
        self.protocol.hangup(&call_id, &self.runtime, auth_id, auth_token);
        Ok(())
    }

    pub fn send_raw_message(&self, session_id: &str, message: &str) -> Result<()> {
        let s = self.sessions.lock_or_recover();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        sess.ws_tx.send(Message::Text(message.to_string())).map_err(|_| EndpointError::Other("WS send failed".into()))
    }

    // ─── Accessors ───────────────────────────────────────────────────────

    pub fn incoming_rx(&self, session_id: &str) -> Result<Receiver<AudioFrame>> {
        let s = self.sessions.lock_or_recover();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        Ok(sess.incoming_rx.clone())
    }

    pub fn checkpoint_notify(&self, session_id: &str) -> Result<Arc<(Mutex<Option<String>>, Condvar)>> {
        let s = self.sessions.lock_or_recover();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        Ok(sess.checkpoint_notify.clone())
    }

    pub fn queued_frames(&self, session_id: &str) -> Result<usize> {
        let spf = (self.config.output_sample_rate * 20 / 1000) as usize;
        let s = self.sessions.lock_or_recover();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        Ok(sess.audio_buf.len() / spf)
    }

    pub fn queued_duration_ms(&self, session_id: &str) -> Result<f64> {
        let s = self.sessions.lock_or_recover();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        Ok(sess.audio_buf.queued_duration_ms(self.config.output_sample_rate))
    }

    /// Register an async_id for "buffer drained to empty" notification.
    ///
    /// - Returns `Ok(None)` if buffer is already empty — emits
    ///   `AudioPlayoutComplete` immediately (caller awaits on the event
    ///   channel; the await resolves on the next event-loop tick).
    ///
    /// **Always returns `Ok(async_id)`** — the completion event always
    /// fires (immediately if buffer already empty, deferred if not).
    /// Multiple concurrent waiters are supported — each gets its own
    /// async_id and all resolve when the buffer next reaches empty.
    pub fn wait_for_playout_async(&self, session_id: &str) -> Result<u64> {
        let async_id = self.async_id_counter.fetch_add(1, Ordering::Relaxed);
        let audio_buf = {
            let s = self.sessions.lock_or_recover();
            let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
            // Terminated session: short-circuit with immediate completion.
            // See `send_audio_async` above for the architectural rationale.
            if sess.terminated.load(Ordering::Acquire) {
                let _ = self.event_tx.try_send(EndpointEvent::AudioPlayoutComplete {
                    session_id: session_id.to_string(),
                    async_id,
                });
                return Ok(async_id);
            }
            sess.audio_buf.clone()
        };
        audio_buf.add_pending_playout(async_id);
        Ok(async_id)
    }

    pub fn input_sample_rate(&self) -> u32 { self.config.input_sample_rate }
    pub fn output_sample_rate(&self) -> u32 { self.config.output_sample_rate }
    pub fn events(&self) -> Receiver<EndpointEvent> { self.event_rx.clone() }

    pub fn shutdown(&self) -> Result<()> {
        if self.cancel.is_cancelled() { return Ok(()); }
        self.cancel.cancel();
        if self.config.auto_hangup {
            let ids: Vec<String> = self.sessions.lock_or_recover().keys().cloned().collect();
            let had_sessions = !ids.is_empty();
            for id in ids { let _ = self.hangup(&id); }
            // hangup() now enqueues the Plivo REST DELETE (and the WS Close frame
            // is sent via the per-session writer task) as DETACHED async work on
            // `self.runtime`. On shutdown the runtime is about to be dropped,
            // which would abort those tasks before they flush. Give them a brief,
            // bounded window to complete so calls tear down cleanly on Plivo's
            // side.
            if had_sessions {
                // Best-effort flush window, deliberately sub-second so shutdown
                // stays fast. This is NOT the per-request hangup ceiling
                // (connect 2s / total 5s in `plivo.rs`): a slow provider response
                // is cut off when the runtime drops just below — an accepted
                // teardown-only tradeoff that never affects live-call audio.
                const HANGUP_DRAIN: std::time::Duration =
                    std::time::Duration::from_millis(750);
                let _ = self.runtime.block_on(async {
                    tokio::time::sleep(HANGUP_DRAIN).await;
                });
            }
        }
        // Push a Shutdown sentinel so adapters blocked on wait_for_event
        // wake immediately rather than waiting for the next poll timeout.
        let _ = self.event_tx.try_send(EndpointEvent::Shutdown);
        info!("Audio streaming shut down");
        Ok(())
    }
}

impl Drop for AudioStreamEndpoint { fn drop(&mut self) { let _ = self.shutdown(); } }

// ─── WebSocket server ────────────────────────────────────────────────────────

async fn run_ws_server(
    addr: &str,
    sessions: Arc<Mutex<HashMap<String, StreamSession>>>,
    etx: Sender<EndpointEvent>,
    cancel: CancellationToken,
    input_sample_rate: u32, output_sample_rate: u32,
    protocol: Arc<dyn StreamProtocol>,
    recording_mgr: Arc<crate::recorder::RecordingManager>,
) -> std::result::Result<(), Box<dyn std::error::Error>> {
    let listener = TcpListener::bind(addr).await?;
    loop {
        tokio::select! {
            _ = cancel.cancelled() => break,
            result = listener.accept() => {
                let (mut stream, peer) = match result {
                    Ok(v) => v,
                    Err(e) => { warn!("TCP accept error: {}", e); continue; }
                };
                info!("WS connection from {}", peer);
                let sid = format!("ws-{:016x}", rand::random::<u64>());
                let (s, e, c, p, r) = (sessions.clone(), etx.clone(), cancel.clone(), protocol.clone(), recording_mgr.clone());
                tokio::spawn(async move {
                    // Peek at the request to detect plain HTTP health checks (e.g., ALB).
                    // If the request lacks "upgrade" header, respond with HTTP 200 and close.
                    // This lets ALB health checks pass on the WebSocket port.
                    let mut peek_buf = [0u8; 512];
                    let n = match stream.peek(&mut peek_buf).await {
                        Ok(n) => n,
                        Err(_) => { return; }
                    };
                    let peek_str = String::from_utf8_lossy(&peek_buf[..n]);
                    if !peek_str.to_ascii_lowercase().contains("upgrade") {
                        // Plain HTTP request (no WebSocket upgrade) — respond with 200 OK
                        use tokio::io::AsyncWriteExt;
                        let resp = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK";
                        let _ = stream.try_write(resp);
                        let _ = stream.shutdown().await;
                        debug!("Health check from {} — responded 200 OK", peer);
                        return;
                    }

                    let ws = match tokio_tungstenite::accept_async(stream).await {
                        Ok(ws) => ws,
                        Err(e) => { warn!("WS handshake failed from {}: {}", peer, e); return; }
                    };
                    handle_ws(ws, sid, s, e, c, input_sample_rate, output_sample_rate, p, r).await;
                });
            }
        }
    }
    Ok(())
}

// ─── Per-connection WebSocket handler ────────────────────────────────────────

async fn handle_ws(
    ws: tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>,
    sid: String,
    sessions: Arc<Mutex<HashMap<String, StreamSession>>>,
    etx: Sender<EndpointEvent>,
    cancel: CancellationToken,
    input_sample_rate: u32, output_sample_rate: u32,
    protocol: Arc<dyn StreamProtocol>,
    recording_mgr: Arc<crate::recorder::RecordingManager>,
) {
    use futures_util::{SinkExt, StreamExt};
    let (mut sink, mut stream) = ws.split();
    let (ws_tx, mut ws_rx) = tokio::sync::mpsc::unbounded_channel::<Message>();
    let (itx, irx) = crossbeam_channel::unbounded();

    let cc = cancel.clone();
    tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = cc.cancelled() => {
                    // Drain any remaining messages before exiting. This
                    // ensures a queued WebSocket Close frame (from hangup)
                    // is flushed to Plivo before we drop the sink. Without
                    // this, the select! might pick cancelled() over recv()
                    // when both are ready, silently discarding the Close.
                    let mut drained = 0u32;
                    while let Ok(m) = ws_rx.try_recv() {
                        let is_close = matches!(m, Message::Close(_));
                        // Timeout: don't hang if WS is dead or Plivo isn't reading.
                        match tokio::time::timeout(
                            std::time::Duration::from_millis(500),
                            sink.send(m),
                        ).await {
                            Ok(Ok(())) => {
                                drained += 1;
                                if is_close {
                                    info!("WS writer: flushed Close frame to Plivo (drained {} msgs)", drained);
                                }
                            }
                            _ => {
                                debug!("WS writer: drain send failed/timed out, stopping");
                                break;
                            }
                        }
                    }
                    if drained > 0 {
                        debug!("WS writer: drained {} remaining messages on cancel", drained);
                    }
                    break;
                }
                msg = ws_rx.recv() => {
                    match msg { Some(m) => { if sink.send(m).await.is_err() { break; } }, None => break }
                }
            }
        }
    });

    let mut encoding = WireEncoding::MulawRate8k;
    let mut upsampler: Option<crate::sip::resampler::Resampler> = None;
    let mut media_recorder: Option<Arc<Mutex<Option<Arc<crate::recorder::CallRecorder>>>>> = None;
    let mut media_beep_det: Option<Arc<Mutex<Option<BeepDetector>>>> = None;
    let mut media_active_sent = false;

    loop {
        tokio::select! {
            _ = cancel.cancelled() => break,
            msg = stream.next() => {
                let msg = match msg {
                    Some(Ok(Message::Text(t))) => t,
                    Some(Ok(Message::Close(_))) | None => break,
                    _ => continue,
                };

                let event = match protocol.parse_message(&msg) {
                    Some(e) => e,
                    None => continue,
                };

                match event {
                    StreamEvent::Start { call_id, stream_id, encoding: enc, headers } => {
                        // Defensive: a second `start` on this WS connection reuses
                        // `sid` (the HashMap key), so the insert() below would
                        // overwrite — and orphan — the previous session's send-loop
                        // task: its CancellationToken clone is dropped un-signalled,
                        // leaving the old loop running forever, draining/encoding/
                        // sending against the same ws_tx as the new loop and leaking
                        // its AudioBuffers. Plivo sends one start per connection, so
                        // this only guards reconnect/replay/protocol quirks — but the
                        // failure mode is a permanent runaway task. Tear the old one
                        // down first (cleanup_session: cancels the old loop + clears
                        // buffers + stops recording; NO CallTerminated emit, since the
                        // same sid lives on with the new session).
                        {
                            let sessions_g = sessions.lock_or_recover();
                            if let Some(old) = sessions_g.get(&sid) {
                                warn!("duplicate `start` on session {} — tearing down previous", sid);
                                cleanup_session(&sid, old, &recording_mgr);
                            }
                        }
                        encoding = enc;
                        upsampler = None; // Reset for new encoding

                        // CRITICAL: tag AudioBuffer with `sid` (the
                        // ws-prefixed session id used everywhere else),
                        // NOT `call_id` (Plivo UUID). The Python audio
                        // source filters incoming events by
                        // ``session_id == self._id`` where ``self._id``
                        // comes from ``CallSession::new(sid, ...)``
                        // below. A mismatch would silently drop every
                        // ``AudioCaptureComplete`` / ``AudioPlayoutComplete``
                        // event and hang ``capture_frame`` at the 30 s
                        // ``DEFAULT_WAIT_TIMEOUT``.
                        let audio_buf = Arc::new(AudioBuffer::with_queue_size(
                            200, output_sample_rate, sid.clone(), etx.clone(),
                        ));
                        // bg_audio_buf is for background audio (push_no_backpressure
                        // only — no async_id semantics). The event_tx is wired through
                        // for symmetry / future use; Drop emissions are harmless if no
                        // pending ids exist.
                        let bg_audio_buf = Arc::new(AudioBuffer::with_queue_size(
                            200, output_sample_rate, format!("{}-bg", sid), etx.clone(),
                        ));
                        let muted = Arc::new(AtomicBool::new(false));
                        let paused = Arc::new(AtomicBool::new(false));
                        let cp_notify = Arc::new((Mutex::new(None), Condvar::new()));
                        let send_loop_notify = Arc::new(tokio::sync::Notify::new());
                        let session_recorder: Arc<Mutex<Option<Arc<crate::recorder::CallRecorder>>>> = Arc::new(Mutex::new(None));
                        let session_beep_detector: Arc<Mutex<Option<BeepDetector>>> = Arc::new(Mutex::new(None));
                        let pending_flush: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
                        let awaiting_checkpoint: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
                        let session_cancel = CancellationToken::new();

                        // Spawn send loop
                        let wstx = ws_tx.clone();
                        let ab = audio_buf.clone();
                        let bg = bg_audio_buf.clone();
                        let rec_send = session_recorder.clone();
                        let (m, p) = (muted.clone(), paused.clone());
                        let pf = pending_flush.clone();
                        let aw = awaiting_checkpoint.clone();
                        let sc = session_cancel.clone();
                        let stream_id_for_loop = stream_id.clone();
                        // Send loop: 20ms interval pacing (matches SIP RTP send loop).
                        // Drains 20ms of audio from agent + background buffers, mixes, encodes, sends.
                        // No checkpoint blocking — audio flows at real-time pace.
                        let chunk_spf: usize = (output_sample_rate * 20 / 1000) as usize; // 20ms chunks
                        let send_proto = protocol.clone();
                        let send_enc = encoding;

                        tokio::spawn(async move {
                            let mut resampler = crate::sip::resampler::Resampler::new_voip(output_sample_rate, send_enc.sample_rate());
                            let mut interval = tokio::time::interval(Duration::from_millis(20));
                            interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

                            // H3: exit the loop the first time a WS send fails.
                            // If the writer task has died (sink closed, peer RST),
                            // continuing to enqueue would accumulate an unbounded
                            // backlog in the MPSC channel for the rest of the
                            // session. Breaking stops the leak and lets cleanup_session
                            // finish the session cleanly.
                            macro_rules! ws_send_or_break {
                                ($msg:expr) => {
                                    if wstx.send($msg).is_err() {
                                        debug!("Send loop: ws_tx closed, exiting");
                                        break;
                                    }
                                };
                            }

                            loop {
                                tokio::select! {
                                    _ = sc.cancelled() => break,
                                    _ = interval.tick() => {}
                                }

                                // Drain background audio regardless of pause state
                                let bg_samples = bg.drain(chunk_spf);
                                // M1: Acquire load so we see all prior Release writes
                                // to the control flags before processing this tick.
                                let muted = m.load(Ordering::Acquire);

                                if p.load(Ordering::Acquire) {
                                    // Paused: send background audio only (no agent voice).
                                    // Record what the caller hears — bg audio (or silence if muted/no bg).
                                    let audible_bg = !bg_samples.is_empty() && !muted;
                                    if audible_bg {
                                        let encoded = send_enc.encode(&bg_samples, &mut resampler);
                                        let play_msg = send_proto.build_play_audio(&encoded, send_enc, &stream_id_for_loop);
                                        ws_send_or_break!(Message::Text(play_msg));
                                    }
                                    {
                                        let guard = rec_send.lock_or_recover();
                                        if let Some(ref rec) = *guard {
                                            if audible_bg {
                                                rec.write_agent_samples(&bg_samples);
                                            } else {
                                                rec.write_agent_samples(&vec![0i16; chunk_spf]);
                                            }
                                        }
                                    }
                                    // If agent buffer already drained before pause, send pending checkpoint
                                    if ab.is_empty() {
                                        if let Some(cp_name) = pf.lock_or_recover().take() {
                                            *aw.lock_or_recover() = Some(cp_name.clone());
                                            let cp_msg = send_proto.build_checkpoint(&stream_id_for_loop, &cp_name);
                                            ws_send_or_break!(Message::Text(cp_msg));
                                            debug!("Send loop: flush checkpoint '{}' sent (paused, buffer empty)", cp_name);
                                        }
                                    }
                                    continue;
                                }

                                let voice = ab.drain(chunk_spf);
                                let has_voice = !voice.is_empty();
                                let has_bg = !bg_samples.is_empty();

                                // Mix voice + bg once — used for both sending and recording.
                                let mixed: Vec<i16> = if has_voice && has_bg {
                                    let len = voice.len().max(bg_samples.len());
                                    let mut out = Vec::with_capacity(len);
                                    for i in 0..len {
                                        let v = if i < voice.len() { voice[i] as i32 } else { 0 };
                                        let b = if i < bg_samples.len() { bg_samples[i] as i32 } else { 0 };
                                        out.push((v + b).clamp(-32768, 32767) as i16);
                                    }
                                    out
                                } else if has_voice {
                                    voice
                                } else {
                                    bg_samples
                                };

                                // Send to Plivo (skipped when muted — caller hears nothing).
                                let audible = !mixed.is_empty() && !muted;
                                if audible {
                                    let encoded = send_enc.encode(&mixed, &mut resampler);
                                    let play_msg = send_proto.build_play_audio(&encoded, send_enc, &stream_id_for_loop);
                                    ws_send_or_break!(Message::Text(play_msg));
                                }

                                // Record what the caller actually hears — muted = silence.
                                {
                                    let guard = rec_send.lock_or_recover();
                                    if let Some(ref rec) = *guard {
                                        if audible {
                                            rec.write_agent_samples(&mixed);
                                        } else {
                                            rec.write_agent_samples(&vec![0i16; chunk_spf]);
                                        }
                                    }
                                }

                                // After draining voice: if voice buffer is now empty, send the queued
                                // flush checkpoint. This is independent of background audio — the
                                // checkpoint tracks voice playout completion only.
                                // Checkpoint is ordered AFTER the last playAudio message, so Plivo's
                                // playedStream confirms that all voice audio has actually been played.
                                if ab.is_empty() {
                                    if let Some(cp_name) = pf.lock_or_recover().take() {
                                        *aw.lock_or_recover() = Some(cp_name.clone());
                                        let cp_msg = send_proto.build_checkpoint(&stream_id_for_loop, &cp_name);
                                        ws_send_or_break!(Message::Text(cp_msg));
                                        debug!("Send loop: flush checkpoint '{}' sent (buffer drained)", cp_name);
                                    }
                                }
                            }
                        });

                        media_recorder = Some(session_recorder.clone());
                        media_beep_det = Some(session_beep_detector.clone());

                        sessions.lock_or_recover().insert(sid.clone(), StreamSession {
                            call_id: call_id.clone(), stream_id: stream_id.clone(),
                            ws_tx: ws_tx.clone(), incoming_tx: itx.clone(), incoming_rx: irx.clone(),
                            audio_buf, bg_audio_buf,
                            terminated: Arc::new(AtomicBool::new(false)),
                            input_resampler: Arc::new(Mutex::new(None)),
                            extra_headers: headers.clone(), encoding,
                            muted, paused,
                            checkpoint_counter: AtomicU64::new(0), checkpoint_notify: cp_notify,
                            send_loop_notify, pending_flush, awaiting_checkpoint,
                            recorder: session_recorder,
                            beep_detector: session_beep_detector,
                            cancel: session_cancel,
                        });

                        let mut session = crate::sip::call::CallSession::new(sid.clone(), crate::sip::call::CallDirection::Inbound);
                        session.call_uuid = Some(call_id.clone());
                        session.remote_uri = call_id;
                        session.local_uri = stream_id;
                        session.extra_headers = headers;
                        // Plivo's WebSocket `start` event is post-answer by
                        // design — Plivo's media server has already bridged
                        // PSTN↔WS and media flows bidirectionally from this
                        // point. Fire CallAnswered immediately so adapters
                        // can create the agent session right away. No
                        // separate pre-answer event on audio_stream: Plivo
                        // doesn't expose a ringing phase over the protocol.
                        let _ = etx.try_send(EndpointEvent::CallAnswered { session });
                        info!("Session {} started (encoding={:?})", sid, encoding);
                    }

                    StreamEvent::Media { payload } => {
                        // First-media observability counter — no longer a
                        // gating event. Session is already active by this
                        // point (CallAnswered fired on the Start event).
                        if !media_active_sent {
                            media_active_sent = true;
                            debug!("First media frame on session {}", sid);
                        }
                        let pcm = {
                            let native = encoding.decode(&payload);
                            let wire_rate = encoding.sample_rate();
                            if upsampler.is_none() {
                                upsampler = crate::sip::resampler::Resampler::new_voip(wire_rate, input_sample_rate);
                            }
                            if let Some(ref mut us) = upsampler {
                                us.process(&native).to_vec()
                            } else {
                                native
                            }
                        };

                        if let Some(ref rec_ref) = media_recorder {
                            // lock_or_recover (not raw .lock()): a poisoned mutex
                            // must not silently stop recording mid-call — recover
                            // and keep writing, consistent with every other site.
                            let guard = rec_ref.lock_or_recover();
                            if let Some(ref rec) = *guard { rec.write_user_samples(&pcm); }
                        }

                        if let Some(ref bd_ref) = media_beep_det {
                            {
                                let mut g = bd_ref.lock_or_recover();
                                if let Some(ref mut det) = *g {
                                    match det.process_frame(&pcm) {
                                        BeepDetectorResult::Detected(e) => {
                                            let _ = etx.try_send(EndpointEvent::BeepDetected { call_id: sid.clone(), frequency_hz: e.frequency_hz, duration_ms: e.duration_ms });
                                            *g = None;
                                        }
                                        BeepDetectorResult::Timeout => {
                                            let _ = etx.try_send(EndpointEvent::BeepTimeout { call_id: sid.clone() });
                                            *g = None;
                                        }
                                        _ => {}
                                    }
                                }
                            }
                        }

                        let n = pcm.len() as u32;
                        let _ = itx.try_send(AudioFrame { data: pcm, sample_rate: input_sample_rate, num_channels: 1, samples_per_channel: n });
                    }

                    StreamEvent::Dtmf { digit } => {
                        let _ = etx.try_send(EndpointEvent::DtmfReceived { call_id: sid.clone(), digit, method: "audio_stream".into() });
                    }

                    StreamEvent::CheckpointAck { name } => {
                        debug!("Checkpoint '{}' confirmed on session {}", name, sid);
                        if let Some(sess) = sessions.lock_or_recover().get(sid.as_str()) {
                            // Only accept if this matches the checkpoint we're waiting for.
                            // Prevents stale confirmations (from cancelled flushes) from
                            // polluting the condvar for the next flush cycle.
                            let matches = sess.awaiting_checkpoint.lock_or_recover()
                                .as_ref()
                                .map(|expected| *expected == name)
                                .unwrap_or(false);
                            if matches {
                                *sess.awaiting_checkpoint.lock_or_recover() = None;
                                let (lock, cvar) = &*sess.checkpoint_notify;
                                *lock.lock_or_recover() = Some(name);
                                cvar.notify_all();
                            } else {
                                debug!("Ignoring stale checkpoint '{}' on session {} (not awaiting)", name, sid);
                            }
                        }
                    }

                    StreamEvent::BufferCleared => {
                        debug!("Buffer cleared confirmed on session {}", sid);
                    }

                    StreamEvent::PlayFailed { reason } => {
                        warn!("Playback failed on session {}: {}", sid, reason);
                        // Clear stale audio to prevent accumulation after failure
                        if let Some(sess) = sessions.lock_or_recover().get(&sid) {
                            sess.audio_buf.clear();
                        }
                    }

                    StreamEvent::StreamError { reason } => {
                        warn!("Stream error on session {}: {}", sid, reason);
                        // Mark terminated and run cleanup, but DO NOT remove
                        // from the sessions map — let Python's `hangup(sid)`
                        // (called from `_run_session.finally` after the audio
                        // source's `aclose()`) be the canonical release point.
                        // This is the LiveKit-faithful model: session lifetime
                        // = audio source lifetime, not transport lifetime.
                        terminate_and_cleanup(&sid, &sessions, &recording_mgr, &etx,
                            format!("stream error: {}", reason));
                        break;
                    }

                    StreamEvent::MuteStream => {
                        info!("Session {} muted by provider", sid);
                        if let Some(sess) = sessions.lock_or_recover().get(&sid) {
                            sess.muted.store(true, std::sync::atomic::Ordering::Release);
                        }
                    }

                    StreamEvent::UnmuteStream => {
                        info!("Session {} unmuted by provider", sid);
                        if let Some(sess) = sessions.lock_or_recover().get(&sid) {
                            sess.muted.store(false, std::sync::atomic::Ordering::Release);
                        }
                    }

                    StreamEvent::Stop => {
                        info!("Session {} stopped", sid);
                        terminate_and_cleanup(&sid, &sessions, &recording_mgr, &etx,
                            "stream stopped".into());
                        break;
                    }
                }
            }
        }
    }

    // WS disconnect: same two-state transition as StreamError / Stop.
    // See StreamSession::terminated comment + StreamError arm above.
    if terminate_and_cleanup(&sid, &sessions, &recording_mgr, &etx, "ws disconnected".into()) {
        info!("Session {} terminated (WS disconnected); awaiting Python hangup() for HashMap removal", sid);
    }
}

/// Clean up a session — cancel send loop and wake any blocked wait_for_playout.
///
/// H4: Before cancelling the send loop, clear any pending-flush checkpoint so the
/// send loop doesn't exit with a queued-but-unsent checkpoint dangling. Any
/// awaiting checkpoint is also cleared so the late `playedStream` confirmation
/// from the provider (if it arrives after teardown) is ignored instead of
/// resolving a stale waiter. Finally, wake any blocked `wait_for_playout`
/// waiters with a sentinel so Python/Node executor threads return immediately.
/// Mark the session terminated, run resource cleanup, emit ``CallTerminated``.
/// Idempotent — returns true if this call actually performed the
/// transition (i.e. ``terminated`` flipped from false to true).
///
/// Does NOT remove the session from the HashMap; that's reserved for
/// ``hangup(sid)`` (which Python calls after `_run_session.finally`
/// runs `session.aclose()`). This split lets the Python audio source
/// outlive the WS transport — its FFI methods early-return on
/// ``terminated`` instead of raising ``CallNotActive``.
fn terminate_and_cleanup(
    sid: &str,
    sessions: &Arc<Mutex<HashMap<String, StreamSession>>>,
    recording_mgr: &Arc<crate::recorder::RecordingManager>,
    etx: &Sender<EndpointEvent>,
    reason: String,
) -> bool {
    // Hold the sessions guard across cleanup_session. cleanup_session only
    // touches per-session state (no recursive sessions lookups) so this
    // can't deadlock — and the brief hold keeps the terminated flip atomic
    // with the cleanup run.
    let do_emit = {
        let sessions_g = sessions.lock_or_recover();
        let Some(sess) = sessions_g.get(sid) else { return false; };
        if sess.terminated.swap(true, Ordering::AcqRel) {
            // Someone already terminated. Don't double-emit.
            false
        } else {
            cleanup_session(sid, sess, recording_mgr);
            true
        }
    };
    if do_emit {
        let session = crate::sip::call::CallSession::new(sid.to_string(), crate::sip::call::CallDirection::Inbound);
        let _ = etx.try_send(EndpointEvent::CallTerminated { session, reason });
    }
    do_emit
}

fn cleanup_session(session_id: &str, sess: &StreamSession, recording_mgr: &Arc<crate::recorder::RecordingManager>) {
    // Clear pending_flush / awaiting_checkpoint so the send loop's drain
    // path is a no-op on teardown and late provider confirms are dropped.
    *sess.pending_flush.lock_or_recover() = None;
    *sess.awaiting_checkpoint.lock_or_recover() = None;
    // Clear the voice buffer so the send loop doesn't keep draining after cancel.
    sess.audio_buf.clear();
    sess.cancel.cancel();
    // Stop recording — wakes encoder thread for immediate finalization
    recording_mgr.stop(session_id);
    // Wake blocked wait_for_playout so executor threads don't hang for 30s
    let (lock, cvar) = &*sess.checkpoint_notify;
    *lock.lock_or_recover() = Some("_closed".into());
    cvar.notify_all();
}

#[cfg(test)]
mod terminated_state_tests {
    //! Verifies the `terminated`-state short-circuit on FFI methods.
    //!
    //! Architectural invariant under test (mirror of LiveKit's
    //! `rtc.AudioSource.capture_frame` early-return on
    //! `_ffi_handle.disposed`): once a session's `terminated` flag flips
    //! true, audio FFI methods must NOT touch the now-defunct WS / RTP
    //! transport — they short-circuit to a successful no-op so the Python
    //! audio source can outlive the transport between WS-close and Python
    //! `hangup(sid)`. Counterpart in `sip/endpoint.rs` is exercised via
    //! the live VPS integration runs.
    use super::*;
    use crate::audio::AudioFrame;
    use crate::audio_stream::plivo::PlivoProtocol;
    use std::time::Duration;
    use tokio::sync::mpsc as tokio_mpsc;
    use tokio_tungstenite::tungstenite::Message;

    fn make_endpoint() -> AudioStreamEndpoint {
        let cfg = AudioStreamConfig {
            listen_addr: "127.0.0.1:0".into(),
            input_sample_rate: 8000,
            output_sample_rate: 8000,
            auto_hangup: false,
        };
        let proto = Arc::new(PlivoProtocol::new("test".into(), "test".into()));
        AudioStreamEndpoint::new(cfg, proto).expect("endpoint::new")
    }

    /// Build a minimal `StreamSession` and insert it under `sid`. The
    /// fields populated here are exactly those the terminated-state code
    /// paths read; everything else uses harmless defaults.
    fn insert_session(ep: &AudioStreamEndpoint, sid: &str, terminated: bool) {
        let (ws_tx, _ws_rx) = tokio_mpsc::unbounded_channel::<Message>();
        let (incoming_tx, incoming_rx) = crossbeam_channel::unbounded::<AudioFrame>();
        let audio_buf = Arc::new(AudioBuffer::with_queue_size(
            200, 8000, sid.to_string(), ep.event_tx.clone(),
        ));
        let bg_audio_buf = Arc::new(AudioBuffer::with_queue_size(
            200, 8000, sid.to_string(), ep.event_tx.clone(),
        ));
        let sess = StreamSession {
            call_id: format!("call-{}", sid),
            stream_id: format!("stream-{}", sid),
            ws_tx,
            incoming_tx,
            incoming_rx,
            audio_buf,
            bg_audio_buf,
            terminated: Arc::new(AtomicBool::new(terminated)),
            input_resampler: Arc::new(Mutex::new(None)),
            extra_headers: HashMap::new(),
            encoding: WireEncoding::MulawRate8k,
            muted: Arc::new(AtomicBool::new(false)),
            paused: Arc::new(AtomicBool::new(false)),
            checkpoint_counter: AtomicU64::new(0),
            checkpoint_notify: Arc::new((Mutex::new(None), Condvar::new())),
            send_loop_notify: Arc::new(tokio::sync::Notify::new()),
            pending_flush: Arc::new(Mutex::new(None)),
            awaiting_checkpoint: Arc::new(Mutex::new(None)),
            recorder: Arc::new(Mutex::new(None)),
            beep_detector: Arc::new(Mutex::new(None)),
            cancel: CancellationToken::new(),
        };
        ep.sessions.lock_or_recover().insert(sid.to_string(), sess);
    }

    fn drain_capture_complete(ep: &AudioStreamEndpoint, deadline: Duration) -> Vec<(String, u64)> {
        let mut out = Vec::new();
        let start = std::time::Instant::now();
        while start.elapsed() < deadline {
            match ep.event_rx.recv_timeout(Duration::from_millis(50)) {
                Ok(EndpointEvent::AudioCaptureComplete { session_id, async_id, .. }) => {
                    out.push((session_id, async_id));
                }
                Ok(_) => continue,
                Err(_) => break,
            }
        }
        out
    }

    fn drain_playout_complete(ep: &AudioStreamEndpoint, deadline: Duration) -> Vec<(String, u64)> {
        let mut out = Vec::new();
        let start = std::time::Instant::now();
        while start.elapsed() < deadline {
            match ep.event_rx.recv_timeout(Duration::from_millis(50)) {
                Ok(EndpointEvent::AudioPlayoutComplete { session_id, async_id }) => {
                    out.push((session_id, async_id));
                }
                Ok(_) => continue,
                Err(_) => break,
            }
        }
        out
    }

    #[test]
    fn send_audio_async_on_terminated_session_emits_capture_complete_without_buffer_push() {
        let ep = make_endpoint();
        insert_session(&ep, "sid-A", /*terminated=*/ true);

        // Capture the buffer's queued duration before; should stay zero after.
        let queued_before = ep.queued_duration_ms("sid-A").unwrap();
        assert_eq!(queued_before, 0.0);

        let frame = AudioFrame { data: vec![0i16; 2000], sample_rate: 8000, num_channels: 1, samples_per_channel: 2000 };
        let async_id = ep.send_audio_async("sid-A", &frame).expect("send_audio_async ok");

        // Buffer must NOT receive the data — the terminated short-circuit
        // bypasses audio_buf.push entirely.
        let queued_after = ep.queued_duration_ms("sid-A").unwrap();
        assert_eq!(queued_after, 0.0, "terminated session must not buffer audio");

        // AudioCaptureComplete must be emitted with the same async_id.
        let events = drain_capture_complete(&ep, Duration::from_millis(200));
        assert_eq!(events, vec![("sid-A".to_string(), async_id)],
            "terminated send_audio_async must emit exactly one AudioCaptureComplete \
             with the returned async_id (LiveKit's `wait_for(predicate)` contract)");
    }

    #[test]
    fn wait_for_playout_async_on_terminated_session_emits_playout_complete_immediately() {
        let ep = make_endpoint();
        insert_session(&ep, "sid-B", /*terminated=*/ true);

        let async_id = ep.wait_for_playout_async("sid-B").expect("wait_for_playout_async ok");
        let events = drain_playout_complete(&ep, Duration::from_millis(200));
        assert_eq!(events, vec![("sid-B".to_string(), async_id)],
            "terminated wait_for_playout_async must emit AudioPlayoutComplete \
             synchronously (no real buffer to drain)");
    }

    #[test]
    fn clear_buffer_on_terminated_session_is_noop() {
        let ep = make_endpoint();
        insert_session(&ep, "sid-C", /*terminated=*/ true);
        // Must succeed without touching ws_tx (which has no live reader)
        // and without sending clearAudio over the dead WS.
        ep.clear_buffer("sid-C").expect("clear_buffer must succeed on terminated session");
    }

    #[test]
    fn pause_resume_on_terminated_session_are_noops() {
        let ep = make_endpoint();
        insert_session(&ep, "sid-D", /*terminated=*/ true);
        ep.pause("sid-D").expect("pause must succeed on terminated session");
        ep.resume("sid-D").expect("resume must succeed on terminated session");
    }

    #[test]
    fn send_background_audio_on_terminated_session_drops_silently() {
        // Regression for the "184 no-backpressure drop" log lines on every
        // call teardown — without the short-circuit, the bg-audio buffer
        // kept filling between WS-close and Python `_on_session_ended`.
        let ep = make_endpoint();
        insert_session(&ep, "sid-E", /*terminated=*/ true);
        let frame = AudioFrame { data: vec![0i16; 160], sample_rate: 8000, num_channels: 1, samples_per_channel: 160 };
        ep.send_background_audio("sid-E", &frame).expect("send_background_audio ok");
        let sess = ep.sessions.lock_or_recover();
        let session = sess.get("sid-E").unwrap();
        assert_eq!(session.bg_audio_buf.queued_duration_ms(8000), 0.0,
            "terminated send_background_audio must NOT push into bg_audio_buf");
    }

    #[test]
    fn active_session_still_buffers_audio() {
        // Sanity counter-test: an active session (terminated=false) must
        // accept frames normally. Without this we couldn't tell if the
        // terminated check was actually firing vs. a bug that drops all
        // frames.
        let ep = make_endpoint();
        insert_session(&ep, "sid-F", /*terminated=*/ false);
        let frame = AudioFrame { data: vec![0i16; 800], sample_rate: 8000, num_channels: 1, samples_per_channel: 800 };
        ep.send_audio_async("sid-F", &frame).expect("send_audio_async ok");
        let queued = ep.queued_duration_ms("sid-F").unwrap();
        assert!(queued > 0.0, "active session must accumulate buffered audio; got {}", queued);
    }
}
