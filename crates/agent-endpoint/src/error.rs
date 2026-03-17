/// Errors returned by the agent endpoint.
#[derive(Debug, thiserror::Error)]
pub enum EndpointError {
    #[error("SIP error (code {code}): {message}")]
    Pjsua { code: i32, message: String },

    #[error("endpoint not initialized")]
    NotInitialized,

    #[error("already initialized")]
    AlreadyInitialized,

    #[error("not registered")]
    NotRegistered,

    #[error("invalid call ID: {0}")]
    InvalidCallId(i32),

    #[error("call not active: {0}")]
    CallNotActive(i32),

    #[error("no audio available")]
    NoAudio,

    #[error("{0}")]
    Other(String),
}

impl EndpointError {
    /// Create an error from a SIP stack status code.
    #[allow(dead_code)]
    pub(crate) fn from_pj_status(status: i32) -> Self {
        // The SIP stack provides human-readable messages via FFI.
        // We format them in the endpoint module where we have access to FFI.
        Self::Pjsua {
            code: status,
            message: format!("pj_status={}", status),
        }
    }
}

pub type Result<T> = std::result::Result<T, EndpointError>;
