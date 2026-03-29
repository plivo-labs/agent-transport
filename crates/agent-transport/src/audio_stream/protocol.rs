//! Stream protocol abstraction for audio streaming providers.
//!
//! Providers implement `StreamProtocol` to handle their specific WebSocket
//! message formats, audio encoding, and call control APIs. The generic
//! `AudioStreamEndpoint` handles everything else: session management,
//! audio mixing, resampling, recording, beep detection, and pacing.

use std::collections::HashMap;

use audio_codec_algorithms::{decode_ulaw, encode_ulaw};

use crate::sip::resampler::Resampler;

// ─── Wire encoding ──────────────────────────────────────────────────────────

/// Audio encoding negotiated with the streaming provider.
#[derive(Clone, Copy, Debug)]
pub enum WireEncoding {
    MulawRate8k,
    L16Rate8k,
    L16Rate16k,
}

impl WireEncoding {
    /// Native sample rate of this wire encoding.
    pub fn sample_rate(&self) -> u32 {
        match self {
            WireEncoding::L16Rate16k => 16000,
            _ => 8000,
        }
    }

    /// Decode raw wire bytes into PCM i16 samples at the encoding's native rate.
    pub fn decode(&self, raw: &[u8]) -> Vec<i16> {
        match self {
            WireEncoding::L16Rate16k | WireEncoding::L16Rate8k => {
                raw.chunks_exact(2).map(|c| i16::from_le_bytes([c[0], c[1]])).collect()
            }
            WireEncoding::MulawRate8k => {
                raw.iter().map(|&b| decode_ulaw(b)).collect()
            }
        }
    }

    /// Encode PCM i16 samples into wire bytes.
    /// Resamples from pipeline rate to wire rate via speexdsp if needed.
    pub fn encode(&self, samples: &[i16], resampler: &mut Option<Resampler>) -> Vec<u8> {
        let resampled;
        let out = if let Some(ref mut rs) = resampler {
            resampled = rs.process(samples).to_vec();
            &resampled
        } else {
            samples
        };
        match self {
            WireEncoding::L16Rate16k | WireEncoding::L16Rate8k => {
                out.iter().flat_map(|&s| s.to_le_bytes()).collect()
            }
            WireEncoding::MulawRate8k => {
                out.iter().map(|&s| encode_ulaw(s)).collect()
            }
        }
    }
}

// ─── Stream events (provider → endpoint) ────────────────────────────────────

/// Events parsed from incoming WebSocket messages.
pub enum StreamEvent {
    /// Session started — provider sent connection/start info.
    Start {
        call_id: String,
        stream_id: String,
        encoding: WireEncoding,
        headers: HashMap<String, String>,
    },
    /// Incoming audio data (already base64-decoded into raw wire bytes).
    Media {
        payload: Vec<u8>,
    },
    /// DTMF digit received.
    Dtmf {
        digit: char,
    },
    /// Provider confirmed a checkpoint (all audio up to this point has played).
    CheckpointAck {
        name: String,
    },
    /// Provider confirmed buffer was cleared.
    BufferCleared,
    /// Session ended.
    Stop,
}

// ─── Stream protocol trait ──────────────────────────────────────────────────

/// Abstraction over provider-specific WebSocket protocol.
///
/// Implement this trait to add support for a new audio streaming provider.
/// The generic `AudioStreamEndpoint` handles all session management, audio
/// processing (mixing, resampling, recording, beep detection), and pacing.
pub trait StreamProtocol: Send + Sync + 'static {
    /// Parse a WebSocket text message into a typed event.
    /// Returns None for unrecognized or irrelevant messages.
    fn parse_message(&self, msg: &str) -> Option<StreamEvent>;

    /// Build a "send audio" WebSocket message.
    /// `encoded_payload` is the wire-encoded audio bytes (already resampled + encoded).
    fn build_play_audio(&self, encoded_payload: &[u8], encoding: WireEncoding, stream_id: &str) -> String;

    /// Build a checkpoint command. Returns the message to send.
    fn build_checkpoint(&self, stream_id: &str, name: &str) -> String;

    /// Build a "clear audio buffer" command.
    fn build_clear_audio(&self, stream_id: &str) -> String;

    /// Build a "send DTMF" command.
    fn build_send_dtmf(&self, digits: &str) -> String;

    /// Hang up the call via provider's API (REST, WebSocket command, etc.).
    /// Called from a blocking context (tokio runtime).
    fn hangup(&self, call_id: &str, rt: &tokio::runtime::Runtime);
}
