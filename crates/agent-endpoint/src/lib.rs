//! Agent Endpoint — SIP user agent for Plivo.
//!
//! Provides a safe Rust API for making/receiving SIP calls,
//! sending/receiving audio frames, DTMF, and call transfers.
//!
//! # Example
//! ```no_run
//! use agent_endpoint::{SipEndpoint, EndpointConfig, Codec, AudioFrame};
//!
//! let config = EndpointConfig {
//!     sip_server: "sip.plivo.com".into(),
//!     codecs: vec![Codec::Opus, Codec::PCMU],
//!     ..Default::default()
//! };
//!
//! let ep = SipEndpoint::new(config).unwrap();
//! ep.register("user", "pass").unwrap();
//!
//! let call_id = ep.call("sip:+15551234567@sip.plivo.com", None).unwrap();
//! // ... send/receive audio frames ...
//! ep.hangup(call_id).unwrap();
//! ep.shutdown().unwrap();
//! ```

mod audio;
mod beep;
mod call;
mod config;
mod endpoint;
mod error;
mod events;
mod media_port;

pub use audio::AudioFrame;
pub use beep::BeepDetectorConfig;
pub use call::{CallDirection, CallSession, CallState};
pub use config::{Codec, EndpointConfig, TurnConfig};
pub use endpoint::SipEndpoint;
pub use error::EndpointError;
pub use events::EndpointEvent;
