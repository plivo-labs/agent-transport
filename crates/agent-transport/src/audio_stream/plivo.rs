//! Plivo audio streaming protocol implementation.
//!
//! Implements `StreamProtocol` for Plivo's WebSocket audio streaming API.
//! Handles Plivo-specific JSON message format, encoding negotiation,
//! and REST API hangup.

use std::collections::HashMap;

use base64::Engine;
use serde::Deserialize;
use tracing::{info, warn};

use super::protocol::{StreamEvent, StreamProtocol, WireEncoding};

// ─── Plivo JSON message types ───────────────────────────────────────────────

#[derive(Deserialize)]
struct PlivoMessage {
    event: String,
    #[serde(default)] start: Option<PlivoStart>,
    #[serde(default)] media: Option<PlivoMedia>,
    #[serde(default)] dtmf: Option<PlivoDtmf>,
    #[serde(default)] name: Option<String>,
    #[serde(default)] extra_headers: Option<String>,
}

#[derive(Deserialize)]
struct PlivoStart {
    #[serde(rename = "callId")] call_id: String,
    #[serde(rename = "streamId")] stream_id: String,
    #[serde(default, rename = "mediaFormat")] media_format: Option<PlivoMediaFormat>,
}

#[derive(Deserialize, Clone)]
struct PlivoMediaFormat {
    #[serde(default)] encoding: String,
    #[serde(rename = "sampleRate", default)] sample_rate: u32,
}

#[derive(Deserialize)]
struct PlivoMedia {
    payload: String,
}

#[derive(Deserialize)]
struct PlivoDtmf {
    digit: String,
}

// ─── Encoding detection ─────────────────────────────────────────────────────

fn detect_encoding(fmt: &PlivoMediaFormat) -> WireEncoding {
    match (fmt.encoding.as_str(), fmt.sample_rate) {
        (e, 16000) if e.contains("l16") || e.contains("L16") => WireEncoding::L16Rate16k,
        (e, _) if e.contains("l16") || e.contains("L16") => WireEncoding::L16Rate8k,
        _ => WireEncoding::MulawRate8k,
    }
}

fn parse_extra_headers(raw: &str) -> HashMap<String, String> {
    let mut headers = HashMap::new();
    if raw.starts_with('{') {
        if let Ok(p) = serde_json::from_str::<HashMap<String, String>>(raw) {
            return p;
        }
    }
    // Plivo uses semicolon-delimited key=value pairs
    for part in raw.split(';') {
        if let Some((k, v)) = part.split_once('=') {
            headers.insert(k.trim().to_string(), v.trim().to_string());
        }
    }
    headers
}

fn wire_content_type(enc: WireEncoding) -> &'static str {
    match enc {
        WireEncoding::MulawRate8k => "audio/x-mulaw",
        WireEncoding::L16Rate8k | WireEncoding::L16Rate16k => "audio/x-l16",
    }
}

// ─── PlivoProtocol ──────────────────────────────────────────────────────────

/// Plivo audio streaming protocol.
pub struct PlivoProtocol {
    auth_id: String,
    auth_token: String,
}

impl PlivoProtocol {
    pub fn new(auth_id: String, auth_token: String) -> Self {
        Self { auth_id, auth_token }
    }
}

impl StreamProtocol for PlivoProtocol {
    fn parse_message(&self, msg: &str) -> Option<StreamEvent> {
        let plivo: PlivoMessage = serde_json::from_str(msg).ok()?;
        match plivo.event.as_str() {
            "start" => {
                let start = plivo.start?;
                let encoding = start.media_format.as_ref()
                    .map(detect_encoding)
                    .unwrap_or(WireEncoding::MulawRate8k);
                let headers = plivo.extra_headers.as_deref()
                    .map(parse_extra_headers)
                    .unwrap_or_default();
                Some(StreamEvent::Start {
                    call_id: start.call_id,
                    stream_id: start.stream_id,
                    encoding,
                    headers,
                })
            }
            "media" => {
                let media = plivo.media?;
                let raw = base64::engine::general_purpose::STANDARD.decode(&media.payload).ok()?;
                Some(StreamEvent::Media { payload: raw })
            }
            "dtmf" => {
                let dtmf = plivo.dtmf?;
                let digit = dtmf.digit.chars().next()?;
                Some(StreamEvent::Dtmf { digit })
            }
            "playedStream" => {
                let name = plivo.name?;
                Some(StreamEvent::CheckpointAck { name })
            }
            "clearedAudio" => Some(StreamEvent::BufferCleared),
            "stop" => Some(StreamEvent::Stop),
            _ => None,
        }
    }

    fn build_play_audio(&self, encoded_payload: &[u8], encoding: WireEncoding, _stream_id: &str) -> String {
        let b64 = base64::engine::general_purpose::STANDARD.encode(encoded_payload);
        serde_json::to_string(&serde_json::json!({
            "event": "playAudio",
            "media": {
                "contentType": wire_content_type(encoding),
                "sampleRate": encoding.sample_rate(),
                "payload": b64
            }
        })).unwrap_or_default()
    }

    fn build_checkpoint(&self, stream_id: &str, name: &str) -> String {
        serde_json::to_string(&serde_json::json!({
            "event": "checkpoint", "streamId": stream_id, "name": name
        })).unwrap_or_default()
    }

    fn build_clear_audio(&self, stream_id: &str) -> String {
        serde_json::to_string(&serde_json::json!({
            "event": "clearAudio", "streamId": stream_id
        })).unwrap_or_default()
    }

    fn build_send_dtmf(&self, digits: &str) -> String {
        serde_json::to_string(&serde_json::json!({
            "event": "sendDTMF", "dtmf": digits
        })).unwrap_or_default()
    }

    fn hangup(&self, call_id: &str, rt: &tokio::runtime::Runtime) {
        if self.auth_id.is_empty() { return; }
        let (auth_id, auth_token, cid) = (self.auth_id.clone(), self.auth_token.clone(), call_id.to_string());
        rt.block_on(async {
            let url = format!("https://api.plivo.com/v1/Account/{}/Call/{}/", auth_id, cid);
            match reqwest::Client::new().delete(&url).basic_auth(&auth_id, Some(&auth_token)).send().await {
                Ok(r) if r.status().is_success() || r.status().as_u16() == 404 => info!("Call {} hung up", cid),
                Ok(r) => warn!("Hangup: {} {}", r.status(), r.text().await.unwrap_or_default()),
                Err(e) => warn!("Hangup: {}", e),
            }
        });
    }
}
