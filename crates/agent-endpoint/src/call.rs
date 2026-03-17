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
