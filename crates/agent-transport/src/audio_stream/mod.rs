//! WebSocket audio streaming transport — Plivo audio streaming.
//!
//! Requires the `audio-stream` feature flag.

#[cfg(feature = "audio-stream")]
pub mod config;
#[cfg(feature = "audio-stream")]
pub mod endpoint;
