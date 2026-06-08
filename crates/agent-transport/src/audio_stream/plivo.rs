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
    #[serde(default)] reason: Option<String>,
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
    // Try JSON first: {"key": "value"}
    if raw.starts_with('{') {
        if let Ok(p) = serde_json::from_str::<HashMap<String, String>>(raw) {
            return p;
        }
        // Plivo sends a non-standard brace format: {key: value, key2: value2}
        // Strip braces and parse as colon-separated pairs.
        let inner = raw.trim_start_matches('{').trim_end_matches('}');
        for part in inner.split(',') {
            if let Some((k, v)) = part.split_once(':') {
                headers.insert(k.trim().to_string(), v.trim().to_string());
            }
        }
        return headers;
    }
    // Semicolon or comma-delimited key=value pairs:
    // "userId=12345;sessionId=abc" or "agentUuid=xxx,name=Amal"
    for part in raw.split(|c| c == ';' || c == ',') {
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
            "playFailed" => {
                let reason = plivo.reason.unwrap_or_else(|| "unknown".to_string());
                Some(StreamEvent::PlayFailed { reason })
            }
            "error" => {
                let reason = plivo.reason.unwrap_or_else(|| "unknown".to_string());
                Some(StreamEvent::StreamError { reason })
            }
            "muteStream" => Some(StreamEvent::MuteStream),
            "unmuteStream" => Some(StreamEvent::UnmuteStream),
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
        })).unwrap_or_else(|e| { warn!("JSON serialize error: {}", e); String::new() })
    }

    fn build_checkpoint(&self, stream_id: &str, name: &str) -> String {
        serde_json::to_string(&serde_json::json!({
            "event": "checkpoint", "streamId": stream_id, "name": name
        })).unwrap_or_else(|e| { warn!("JSON serialize error: {}", e); String::new() })
    }

    fn build_clear_audio(&self, stream_id: &str) -> String {
        serde_json::to_string(&serde_json::json!({
            "event": "clearAudio", "streamId": stream_id
        })).unwrap_or_else(|e| { warn!("JSON serialize error: {}", e); String::new() })
    }

    fn build_send_dtmf(&self, digits: &str) -> String {
        serde_json::to_string(&serde_json::json!({
            "event": "sendDTMF", "dtmf": digits
        })).unwrap_or_else(|e| { warn!("JSON serialize error: {}", e); String::new() })
    }

    // Plivo does not support muteStream/unmuteStream.
    // Pause uses clearAudio instead (handled in endpoint.rs).

    fn hangup(&self, call_id: &str, rt: &tokio::runtime::Runtime, auth_id_override: Option<&str>, auth_token_override: Option<&str>) {
        let aid = auth_id_override.unwrap_or(&self.auth_id);
        let atk = auth_token_override.unwrap_or(&self.auth_token);
        if aid.is_empty() { return; }
        let (aid, atk, cid) = (aid.to_string(), atk.to_string(), call_id.to_string());
        // Fire-and-forget on the endpoint's own tokio runtime. The REST DELETE
        // runs as a detached async task and this fn returns IMMEDIATELY, so the
        // caller — a PyO3 pymethod invoked from Python — is never blocked for the
        // network round-trip. No Python thread (loop OR executor worker) is held
        // while Plivo is contacted. The completion is logged from inside the task.
        //
        // The timeouts are a safety ceiling for the detached task (nothing awaits
        // it on the hot path); they also bound how long the shutdown drain in
        // `AudioStreamEndpoint::shutdown` must wait before the runtime is dropped.
        rt.spawn(async move {
            let url = format!("https://api.plivo.com/v1/Account/{}/Call/{}/", aid, cid);
            let client = reqwest::Client::builder()
                .connect_timeout(std::time::Duration::from_secs(2))
                .timeout(std::time::Duration::from_secs(5))
                .build()
                .unwrap_or_else(|_| reqwest::Client::new());
            match client.delete(&url).basic_auth(&aid, Some(&atk)).send().await {
                Ok(r) if r.status().is_success() || r.status().as_u16() == 404 => info!("Call {} hung up", cid),
                Ok(r) => warn!("Hangup: {} {}", r.status(), r.text().await.unwrap_or_default()),
                Err(e) => warn!("Hangup: {}", e),
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_plivo_brace_format() {
        let raw = "{X-PH-name: Amal, X-PH-is_fs_krisp_enabled: true}";
        let h = parse_extra_headers(raw);
        assert_eq!(h.get("X-PH-name").unwrap(), "Amal");
        assert_eq!(h.get("X-PH-is_fs_krisp_enabled").unwrap(), "true");
    }

    #[test]
    fn parse_plivo_brace_format_single() {
        let raw = "{X-PH-is_fs_krisp_enabled: true}";
        let h = parse_extra_headers(raw);
        assert_eq!(h.len(), 1);
        assert_eq!(h.get("X-PH-is_fs_krisp_enabled").unwrap(), "true");
    }

    #[test]
    fn parse_json_format() {
        let raw = r#"{"userId": "12345", "sessionId": "abc"}"#;
        let h = parse_extra_headers(raw);
        assert_eq!(h.get("userId").unwrap(), "12345");
        assert_eq!(h.get("sessionId").unwrap(), "abc");
    }

    #[test]
    fn parse_semicolon_delimited() {
        let raw = "userId=12345;sessionId=abc-xyz";
        let h = parse_extra_headers(raw);
        assert_eq!(h.get("userId").unwrap(), "12345");
        assert_eq!(h.get("sessionId").unwrap(), "abc-xyz");
    }

    #[test]
    fn parse_comma_delimited() {
        let raw = "agentUuid=xxx,name=Amal";
        let h = parse_extra_headers(raw);
        assert_eq!(h.get("agentUuid").unwrap(), "xxx");
        assert_eq!(h.get("name").unwrap(), "Amal");
    }

    #[test]
    fn parse_single_pair() {
        let raw = "name=Amal";
        let h = parse_extra_headers(raw);
        assert_eq!(h.len(), 1);
        assert_eq!(h.get("name").unwrap(), "Amal");
    }

    #[test]
    fn parse_empty_json_object() {
        let raw = "{}";
        let h = parse_extra_headers(raw);
        assert!(h.is_empty());
    }

    #[test]
    fn parse_empty_string() {
        let raw = "";
        let h = parse_extra_headers(raw);
        assert!(h.is_empty());
    }
}
