use crate::sip::call::CallSession;

/// Events emitted by the SIP endpoint.
#[derive(Debug, Clone)]
pub enum EndpointEvent {
    /// Successfully registered with the SIP server.
    Registered,

    /// Registration failed.
    RegistrationFailed { error: String },

    /// Unregistered from the SIP server.
    Unregistered,

    /// An inbound SIP call is ringing — `180 Ringing` has been sent to the
    /// peer but the call has NOT been answered yet. Observational event for
    /// adapters that want to inspect caller info (headers, remote URI, call
    /// UUID) and log or screen before the auto-answer runs.
    ///
    /// This event is SIP-inbound-only; Plivo audio_stream has no pre-answer
    /// phase visible over the WebSocket protocol.
    CallRinging { session: CallSession },

    /// Call state changed.
    CallStateChanged { session: CallSession },

    /// Call is answered and media is active. For SIP this fires after
    /// `200 OK` is sent (inbound) or received (outbound) and the RTP send /
    /// receive loops are running. For audio_stream this fires as soon as
    /// Plivo's WebSocket `start` event is parsed — that's already post-
    /// answer by Plivo's architecture.
    ///
    /// Adapters should create the agent session on this event (or on the
    /// synchronous return of `ep.call()` for outbound SIP — same moment in
    /// time, different signalling).
    CallAnswered { session: CallSession },

    /// Call has been terminated.
    CallTerminated {
        session: CallSession,
        reason: String,
    },

    /// A DTMF digit was received.
    DtmfReceived {
        call_id: String,
        digit: char,
        /// "rfc2833" or "sip_info"
        method: String,
    },

    /// A voicemail beep was detected on the call.
    BeepDetected {
        call_id: String,
        frequency_hz: f64,
        duration_ms: u32,
    },

    /// Beep detection timed out — no beep was found.
    BeepTimeout {
        call_id: String,
    },

    /// Endpoint is shutting down. Pushed by `shutdown()` so any blocking
    /// `wait_for_event` callers wake immediately instead of waiting for
    /// the next poll timeout. Adapter event loops should treat this as a
    /// signal to stop dispatching and exit cleanly.
    Shutdown,

    /// A previously-deferred `send_audio_async` has cleared — the buffer
    /// has drained below the backpressure threshold, so the caller may
    /// push more audio without blocking. The `async_id` matches the value
    /// returned by `send_audio_async` so adapters can resolve the right
    /// awaiter.
    ///
    /// Replaces the old `pending_complete` Python callback. The event
    /// pattern is mandatory: Rust threads do NOT invoke Python callbacks,
    /// which was the source of the GIL+Mutex AB-BA deadlock seen in prod.
    ///
    /// `cancelled` is `true` when the completion was synthesized by
    /// `clear_buffer()` (or an equivalent buffer drop / session
    /// teardown) rather than a real RTP/WS send. LiveKit consumers
    /// ignore this field — clear semantics for them are silent-discard,
    /// matching `rtc.AudioSource.clear_queue` behaviour. Pipecat's
    /// `write_audio_frame -> bool` API checks it so the frame counts as
    /// dropped rather than delivered.
    AudioCaptureComplete {
        session_id: String,
        async_id: u64,
        cancelled: bool,
    },

    /// All queued audio for this session has been played out — the buffer
    /// is empty AND (where applicable) the remote endpoint has confirmed
    /// playback (Plivo `playedStream` / RTP send-complete). Matches the
    /// `async_id` returned by `wait_for_playout_async`.
    ///
    /// Replaces the old `playout_callback`.
    AudioPlayoutComplete {
        session_id: String,
        async_id: u64,
    },

    /// The audio buffer has drained completely (independent of any
    /// specific async_id). Used for observational backpressure / metrics;
    /// adapters that care about per-frame completion should use
    /// `AudioCaptureComplete` instead.
    AudioBufferDrained {
        session_id: String,
        async_id: u64,
    },

    /// A pending audio operation (`send_audio_async` /
    /// `wait_for_playout_async`) was cancelled — e.g., the buffer was
    /// cleared via `clear_buffer()`, flushed, or the session went away
    /// while the operation was outstanding. Adapters must surface this as
    /// a cancellation/error to the awaiter rather than hanging forever.
    AudioCaptureError {
        session_id: String,
        async_id: u64,
        error: String,
    },
}

impl EndpointEvent {
    /// Returns the snake_case event name used for callback dispatch.
    pub fn callback_name(&self) -> &'static str {
        match self {
            EndpointEvent::Registered => "registered",
            EndpointEvent::RegistrationFailed { .. } => "registration_failed",
            EndpointEvent::Unregistered => "unregistered",
            EndpointEvent::CallRinging { .. } => "call_ringing",
            EndpointEvent::CallStateChanged { .. } => "call_state",
            EndpointEvent::CallAnswered { .. } => "call_answered",
            EndpointEvent::CallTerminated { .. } => "call_terminated",
            EndpointEvent::DtmfReceived { .. } => "dtmf_received",
            EndpointEvent::BeepDetected { .. } => "beep_detected",
            EndpointEvent::BeepTimeout { .. } => "beep_timeout",
            EndpointEvent::Shutdown => "shutdown",
            EndpointEvent::AudioCaptureComplete { .. } => "audio_capture_complete",
            EndpointEvent::AudioPlayoutComplete { .. } => "audio_playout_complete",
            EndpointEvent::AudioBufferDrained { .. } => "audio_buffer_drained",
            EndpointEvent::AudioCaptureError { .. } => "audio_capture_error",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sip::call::{CallDirection, CallSession};

    #[test]
    fn test_callback_names() {
        assert_eq!(EndpointEvent::Registered.callback_name(), "registered");
        assert_eq!(
            EndpointEvent::RegistrationFailed {
                error: "x".into()
            }
            .callback_name(),
            "registration_failed"
        );
        assert_eq!(EndpointEvent::Unregistered.callback_name(), "unregistered");
        let session = CallSession::new("test-0".into(), CallDirection::Inbound);
        assert_eq!(
            EndpointEvent::CallRinging {
                session: session.clone()
            }
            .callback_name(),
            "call_ringing"
        );
        assert_eq!(
            EndpointEvent::CallStateChanged {
                session: session.clone()
            }
            .callback_name(),
            "call_state"
        );
        assert_eq!(
            EndpointEvent::CallAnswered {
                session: session.clone()
            }
            .callback_name(),
            "call_answered"
        );
        assert_eq!(
            EndpointEvent::CallTerminated {
                session,
                reason: "bye".into()
            }
            .callback_name(),
            "call_terminated"
        );
        assert_eq!(
            EndpointEvent::DtmfReceived {
                call_id: "0".into(),
                digit: '1',
                method: "rfc2833".into()
            }
            .callback_name(),
            "dtmf_received"
        );
        assert_eq!(
            EndpointEvent::BeepDetected {
                call_id: "0".into(),
                frequency_hz: 1000.0,
                duration_ms: 100
            }
            .callback_name(),
            "beep_detected"
        );
        assert_eq!(
            EndpointEvent::BeepTimeout { call_id: "0".into() }.callback_name(),
            "beep_timeout"
        );
        assert_eq!(
            EndpointEvent::AudioCaptureComplete {
                session_id: "s".into(),
                async_id: 42,
                cancelled: false,
            }
            .callback_name(),
            "audio_capture_complete"
        );
        assert_eq!(
            EndpointEvent::AudioPlayoutComplete {
                session_id: "s".into(),
                async_id: 7,
            }
            .callback_name(),
            "audio_playout_complete"
        );
        assert_eq!(
            EndpointEvent::AudioBufferDrained {
                session_id: "s".into(),
                async_id: 1,
            }
            .callback_name(),
            "audio_buffer_drained"
        );
        assert_eq!(
            EndpointEvent::AudioCaptureError {
                session_id: "s".into(),
                async_id: 99,
                error: "cleared".into(),
            }
            .callback_name(),
            "audio_capture_error"
        );
    }
}
