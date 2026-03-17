use crate::audio::AudioFrame;
use crate::call::CallSession;

/// Events emitted by the SIP endpoint.
#[derive(Debug, Clone)]
pub enum EndpointEvent {
    /// Successfully registered with the SIP server.
    Registered,

    /// Registration failed.
    RegistrationFailed { error: String },

    /// Unregistered from the SIP server.
    Unregistered,

    /// An incoming call is ringing.
    IncomingCall { session: CallSession },

    /// Call state changed.
    CallStateChanged { session: CallSession },

    /// Call media (audio) is now active.
    CallMediaActive { call_id: i32 },

    /// Call has been terminated.
    CallTerminated {
        session: CallSession,
        reason: String,
    },

    /// An audio frame was received from a call.
    AudioReceived {
        call_id: i32,
        frame: AudioFrame,
    },

    /// A DTMF digit was received.
    DtmfReceived {
        call_id: i32,
        digit: char,
        /// "rfc2833" or "sip_info"
        method: String,
    },

    /// A voicemail beep was detected on the call.
    BeepDetected {
        call_id: i32,
        frequency_hz: f64,
        duration_ms: u32,
    },

    /// Beep detection timed out — no beep was found.
    BeepTimeout {
        call_id: i32,
    },
}
