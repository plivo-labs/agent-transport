//! Agent Transport — multi-transport library for AI voice agents.
//!
//! Supports:
//! - **SIP transport** (`sip` module): Direct SIP calling via rsipstack + RTP
//! - **Audio streaming** (`audio_stream` module): Plivo WebSocket audio streaming
//!
//! Both transports produce/consume the same `AudioFrame` format (int16 PCM, 16kHz mono).
//!
//! # Example (SIP)
//! ```no_run
//! use agent_transport::{SipEndpoint, EndpointConfig, Codec, AudioFrame};
//!
//! let config = EndpointConfig {
//!     sip_server: "phone.plivo.com".into(),
//!     codecs: vec![Codec::PCMU, Codec::PCMA],
//!     ..Default::default()
//! };
//!
//! let ep = SipEndpoint::new(config).unwrap();
//! ep.register("user", "pass").unwrap();
//! let call_id = ep.call("sip:+15551234567@phone.plivo.com", None).unwrap();
//! ep.hangup(call_id).unwrap();
//! ep.shutdown().unwrap();
//! ```

// Shared modules
mod audio;
#[cfg(feature = "comfort-noise")]
pub mod comfort_noise;
mod config;
mod error;
mod events;
#[cfg(feature = "plc")]
pub mod plc;
mod recorder;

// Transport modules
pub mod sip;
pub mod audio_stream;

// ─── Public re-exports (backward compatible) ─────────────────────────────────

// Shared types
pub use audio::AudioFrame;
pub use beep_detector::BeepDetectorConfig;
pub use config::{AudioProcessingConfig, Codec, EndpointConfig, TurnConfig};
pub use error::EndpointError;
pub use events::EndpointEvent;

// SIP transport (re-exported at root for backward compat)
pub use sip::call::{CallDirection, CallSession, CallState};
pub use sip::endpoint::SipEndpoint;
