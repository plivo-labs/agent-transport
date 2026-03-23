//! SIP transport — register, make/receive calls, send/receive audio over RTP.

pub mod call;
pub(crate) mod dtmf;
pub mod endpoint;
pub(crate) mod rtcp;
pub(crate) mod audio_buffer;
pub mod resampler;
pub(crate) mod rtp_transport;
pub(crate) mod sdp;
