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

impl EndpointEvent {
    /// Returns the snake_case event name used for callback dispatch.
    pub fn callback_name(&self) -> &'static str {
        match self {
            EndpointEvent::Registered => "registered",
            EndpointEvent::RegistrationFailed { .. } => "registration_failed",
            EndpointEvent::Unregistered => "unregistered",
            EndpointEvent::IncomingCall { .. } => "incoming_call",
            EndpointEvent::CallStateChanged { .. } => "call_state",
            EndpointEvent::CallMediaActive { .. } => "call_media_active",
            EndpointEvent::CallTerminated { .. } => "call_terminated",
            EndpointEvent::DtmfReceived { .. } => "dtmf_received",
            EndpointEvent::BeepDetected { .. } => "beep_detected",
            EndpointEvent::BeepTimeout { .. } => "beep_timeout",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::call::{CallDirection, CallSession};

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
        let session = CallSession::new(0, CallDirection::Inbound);
        assert_eq!(
            EndpointEvent::IncomingCall {
                session: session.clone()
            }
            .callback_name(),
            "incoming_call"
        );
        assert_eq!(
            EndpointEvent::CallStateChanged {
                session: session.clone()
            }
            .callback_name(),
            "call_state"
        );
        assert_eq!(
            EndpointEvent::CallMediaActive { call_id: 0 }.callback_name(),
            "call_media_active"
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
                call_id: 0,
                digit: '1',
                method: "rfc2833".into()
            }
            .callback_name(),
            "dtmf_received"
        );
        assert_eq!(
            EndpointEvent::BeepDetected {
                call_id: 0,
                frequency_hz: 1000.0,
                duration_ms: 100
            }
            .callback_name(),
            "beep_detected"
        );
        assert_eq!(
            EndpointEvent::BeepTimeout { call_id: 0 }.callback_name(),
            "beep_timeout"
        );
    }
}
