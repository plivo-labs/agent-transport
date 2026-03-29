//! WebSocket audio streaming transport.
//!
//! Provider-agnostic core with pluggable protocol implementations.
//! Built-in: Plivo audio streaming (`plivo` module).
//!
//! Requires the `audio-stream` feature flag.

#[cfg(feature = "audio-stream")]
pub mod config;
#[cfg(feature = "audio-stream")]
pub mod protocol;
#[cfg(feature = "audio-stream")]
pub mod endpoint;
#[cfg(feature = "audio-stream")]
pub mod plivo;
