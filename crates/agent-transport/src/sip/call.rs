use std::collections::HashMap;

/// Direction of a call.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CallDirection {
    Inbound,
    Outbound,
}

/// State of a call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CallState {
    /// INVITE sent, waiting for response
    Calling,
    /// Incoming call, not yet answered
    Incoming,
    /// 180 Ringing or 183 Session Progress received
    Early,
    /// 200 OK received, completing handshake
    Connecting,
    /// ACK sent/received, media is active
    Confirmed,
    /// Call ended normally
    Disconnected,
    /// Call failed with a reason
    Failed(String),
}

impl CallState {
    /// Whether the call is still active (not disconnected or failed).
    pub fn is_active(&self) -> bool {
        !matches!(self, CallState::Disconnected | CallState::Failed(_))
    }
}

/// Represents a SIP call session.
#[derive(Debug, Clone)]
pub struct CallSession {
    /// Internal call ID
    pub call_id: i32,

    /// Plivo's X-CallUUID if present
    pub call_uuid: Option<String>,

    /// Call direction
    pub direction: CallDirection,

    /// Current call state
    pub state: CallState,

    /// Remote party URI (e.g., "sip:+15551234567@sip.plivo.com")
    pub remote_uri: String,

    /// Local URI
    pub local_uri: String,

    /// Custom SIP headers from the INVITE
    pub extra_headers: HashMap<String, String>,
}

impl CallSession {
    pub(crate) fn new(call_id: i32, direction: CallDirection) -> Self {
        Self {
            call_id,
            call_uuid: None,
            direction,
            state: match direction {
                CallDirection::Outbound => CallState::Calling,
                CallDirection::Inbound => CallState::Incoming,
            },
            remote_uri: String::new(),
            local_uri: String::new(),
            extra_headers: HashMap::new(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_new_outbound_call() {
        let session = CallSession::new(0, CallDirection::Outbound);
        assert_eq!(session.state, CallState::Calling);
        assert_eq!(session.direction, CallDirection::Outbound);
        assert_eq!(session.call_id, 0);
        assert!(session.call_uuid.is_none());
        assert!(session.extra_headers.is_empty());
        assert!(session.remote_uri.is_empty());
    }

    #[test]
    fn test_new_inbound_call() {
        let session = CallSession::new(1, CallDirection::Inbound);
        assert_eq!(session.state, CallState::Incoming);
        assert_eq!(session.direction, CallDirection::Inbound);
        assert_eq!(session.call_id, 1);
    }

    #[test]
    fn test_call_state_is_active() {
        assert!(CallState::Calling.is_active());
        assert!(CallState::Incoming.is_active());
        assert!(CallState::Early.is_active());
        assert!(CallState::Connecting.is_active());
        assert!(CallState::Confirmed.is_active());
        assert!(!CallState::Disconnected.is_active());
        assert!(!CallState::Failed("err".into()).is_active());
    }
}
