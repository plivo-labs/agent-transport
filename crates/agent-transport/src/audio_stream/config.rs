//! Configuration for Plivo WebSocket audio streaming transport.

/// Configuration for the audio streaming endpoint.
#[derive(Debug, Clone)]
pub struct AudioStreamConfig {
    /// Address to listen on for WebSocket connections (e.g., "0.0.0.0:8080").
    pub listen_addr: String,
    /// Plivo Auth ID (for REST API hangup).
    pub plivo_auth_id: String,
    /// Plivo Auth Token (for REST API hangup).
    pub plivo_auth_token: String,
    /// Pipeline sample rate in Hz (default: 8000).
    /// Audio is received at 8kHz from Plivo and resampled to this rate if different.
    pub sample_rate: u32,
    /// Automatically hang up the call on shutdown (default: true).
    pub auto_hangup: bool,
}

impl Default for AudioStreamConfig {
    fn default() -> Self {
        Self {
            listen_addr: "0.0.0.0:8080".into(),
            plivo_auth_id: String::new(),
            plivo_auth_token: String::new(),
            sample_rate: 8000,
            auto_hangup: true,
        }
    }
}
