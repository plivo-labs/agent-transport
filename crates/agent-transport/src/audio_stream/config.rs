//! Configuration for WebSocket audio streaming transport.

/// Configuration for the audio streaming endpoint.
///
/// Provider-specific settings (auth credentials, etc.) are passed via the
/// `StreamProtocol` implementation, not this config.
#[derive(Debug, Clone)]
pub struct AudioStreamConfig {
    /// Address to listen on for WebSocket connections (e.g., "0.0.0.0:8080").
    pub listen_addr: String,
    /// Pipeline sample rate in Hz (default: 8000).
    /// Audio is received from the provider and resampled to this rate if different.
    pub sample_rate: u32,
    /// Automatically hang up the call on shutdown (default: true).
    pub auto_hangup: bool,
}

impl Default for AudioStreamConfig {
    fn default() -> Self {
        Self {
            listen_addr: "0.0.0.0:8080".into(),
            sample_rate: 8000,
            auto_hangup: true,
        }
    }
}
