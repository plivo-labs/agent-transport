/// Errors returned by the agent transport.
#[derive(Debug, thiserror::Error)]
pub enum EndpointError {
    #[error("SIP error (code {code}): {message}")]
    Sip { code: i32, message: String },

    #[error("endpoint not initialized")]
    NotInitialized,

    #[error("already initialized")]
    AlreadyInitialized,

    #[error("not registered")]
    NotRegistered,

    #[error("invalid call ID: {0}")]
    InvalidCallId(String),

    #[error("call not active: {0}")]
    CallNotActive(String),

    #[error("no audio available")]
    NoAudio,

    #[error("{0}")]
    Other(String),
}

pub type Result<T> = std::result::Result<T, EndpointError>;
