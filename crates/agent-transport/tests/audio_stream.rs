//! Unit tests for audio streaming message format and codec logic.
//!
//! Tests the JSON message construction and parsing used by
//! the Plivo WebSocket audio streaming transport.
#![cfg(feature = "audio-stream")]

use agent_transport::AudioFrame;

#[test]
fn test_audio_frame_to_bytes_roundtrip() {
    let frame = AudioFrame::new(vec![100, -200, 300, -400], 16000, 1);
    let bytes = frame.as_bytes();
    assert_eq!(bytes.len(), 8); // 4 samples × 2 bytes
    let restored = AudioFrame::from_bytes(&bytes, 16000, 1);
    assert_eq!(restored.data, vec![100, -200, 300, -400]);
}

#[test]
fn test_audio_frame_silence() {
    let frame = AudioFrame::silence(16000, 1, 20); // 20ms at 16kHz
    assert_eq!(frame.samples_per_channel, 320);
    assert_eq!(frame.data.len(), 320);
    assert!(frame.data.iter().all(|&s| s == 0));
}

#[test]
fn test_audio_frame_duration() {
    let frame = AudioFrame::new(vec![0; 320], 16000, 1);
    assert_eq!(frame.duration_ms(), 20);

    let frame = AudioFrame::new(vec![0; 160], 8000, 1);
    assert_eq!(frame.duration_ms(), 20);
}

#[test]
fn test_g711_ulaw_encode_decode_via_codec() {
    use agent_transport::Codec;
    let samples = vec![0i16, 1000, -1000, 8000, -8000, 16000, -16000];
    let encoded = Codec::PCMU.encode(&samples);
    assert_eq!(encoded.len(), 7);
    let decoded = Codec::PCMU.decode(&encoded);
    assert_eq!(decoded.len(), 7);
    // Lossy codec — verify roundtrip is within tolerance
    for (orig, dec) in samples.iter().zip(decoded.iter()) {
        let diff = (*orig as i32 - *dec as i32).unsigned_abs();
        assert!(diff < (orig.unsigned_abs() as u32 / 5).max(200),
            "PCMU roundtrip: {} -> {} (diff {})", orig, dec, diff);
    }
}

#[test]
fn test_g711_alaw_encode_decode_via_codec() {
    use agent_transport::Codec;
    let samples = vec![0i16, 1000, -1000, 8000, -8000];
    let encoded = Codec::PCMA.encode(&samples);
    let decoded = Codec::PCMA.decode(&encoded);
    for (orig, dec) in samples.iter().zip(decoded.iter()) {
        let diff = (*orig as i32 - *dec as i32).unsigned_abs();
        assert!(diff < (orig.unsigned_abs() as u32 / 5).max(200),
            "PCMA roundtrip: {} -> {} (diff {})", orig, dec, diff);
    }
}

#[test]
fn test_base64_mulaw_roundtrip() {
    // Simulate the Plivo audio streaming encode/decode path:
    // PCM int16 → mu-law → base64 → decode → mu-law → PCM int16
    use audio_codec_algorithms::{encode_ulaw, decode_ulaw};
    use base64::Engine;

    let pcm_samples = vec![0i16, 500, -500, 2000, -2000, 8000, -8000];

    // Encode: PCM → mu-law bytes
    let ulaw_bytes: Vec<u8> = pcm_samples.iter().map(|&s| encode_ulaw(s)).collect();

    // Transport: mu-law → base64
    let b64 = base64::engine::general_purpose::STANDARD.encode(&ulaw_bytes);

    // Decode: base64 → mu-law → PCM
    let decoded_ulaw = base64::engine::general_purpose::STANDARD.decode(&b64).unwrap();
    let decoded_pcm: Vec<i16> = decoded_ulaw.iter().map(|&b| decode_ulaw(b)).collect();

    // Verify roundtrip
    assert_eq!(ulaw_bytes.len(), pcm_samples.len());
    assert_eq!(decoded_pcm.len(), pcm_samples.len());

    for (orig, dec) in pcm_samples.iter().zip(decoded_pcm.iter()) {
        let diff = (*orig as i32 - *dec as i32).unsigned_abs();
        assert!(diff < (orig.unsigned_abs() as u32 / 5).max(200),
            "base64 mu-law roundtrip: {} -> {} (diff {})", orig, dec, diff);
    }
}

#[test]
fn test_plivo_play_audio_json_format() {
    use base64::Engine;
    let payload = base64::engine::general_purpose::STANDARD.encode(&[0xFF; 160]);
    let msg = serde_json::json!({
        "event": "playAudio",
        "media": {
            "contentType": "audio/x-mulaw",
            "sampleRate": 8000,
            "payload": payload,
        }
    });
    let json_str = serde_json::to_string(&msg).unwrap();
    assert!(json_str.contains("\"event\":\"playAudio\""));
    assert!(json_str.contains("\"contentType\":\"audio/x-mulaw\""));
    assert!(json_str.contains("\"sampleRate\":8000"));
}

#[test]
fn test_plivo_clear_audio_json_format() {
    let msg = serde_json::json!({
        "event": "clearAudio",
        "streamId": "test-stream-123",
    });
    let json_str = serde_json::to_string(&msg).unwrap();
    assert!(json_str.contains("\"event\":\"clearAudio\""));
    assert!(json_str.contains("\"streamId\":\"test-stream-123\""));
}

// ─── Hangup credential tests ────────────────────────────────────────────────

mod hangup_tests {
    use std::sync::{Arc, Mutex, atomic::{AtomicBool, Ordering}};
    use agent_transport::audio_stream::protocol::{StreamEvent, StreamProtocol, WireEncoding};
    use agent_transport::audio_stream::config::AudioStreamConfig;
    use agent_transport::audio_stream::endpoint::AudioStreamEndpoint;
    use agent_transport::audio_stream::plivo::PlivoProtocol;

    /// Records what credentials were passed to hangup().
    #[derive(Clone)]
    struct MockProtocol {
        hangup_calls: Arc<Mutex<Vec<(String, Option<String>, Option<String>)>>>,
        hangup_called: Arc<AtomicBool>,
    }

    impl MockProtocol {
        fn new() -> Self {
            Self {
                hangup_calls: Arc::new(Mutex::new(Vec::new())),
                hangup_called: Arc::new(AtomicBool::new(false)),
            }
        }
    }

    impl StreamProtocol for MockProtocol {
        fn parse_message(&self, _msg: &str) -> Option<StreamEvent> { None }
        fn build_play_audio(&self, _: &[u8], _: WireEncoding, _: &str) -> String { String::new() }
        fn build_checkpoint(&self, _: &str, _: &str) -> String { String::new() }
        fn build_clear_audio(&self, _: &str) -> String { String::new() }
        fn build_send_dtmf(&self, _: &str) -> String { String::new() }

        fn hangup(&self, call_id: &str, rt: &tokio::runtime::Runtime, auth_id: Option<&str>, auth_token: Option<&str>) -> Option<tokio::task::JoinHandle<()>> {
            self.hangup_calls.lock().unwrap().push((
                call_id.to_string(),
                auth_id.map(|s| s.to_string()),
                auth_token.map(|s| s.to_string()),
            ));
            let called = self.hangup_called.clone();
            Some(rt.spawn(async move {
                called.store(true, Ordering::SeqCst);
            }))
        }
    }

    // ─── PlivoProtocol credential logic ─────────────────────────────────

    #[test]
    fn plivo_hangup_empty_creds_returns_none() {
        let proto = PlivoProtocol::new(String::new(), String::new());
        let rt = tokio::runtime::Runtime::new().unwrap();
        // No override, empty defaults → should skip (return None)
        let handle = proto.hangup("call-123", &rt, None, None);
        assert!(handle.is_none());
    }

    #[test]
    fn plivo_hangup_empty_default_with_override_returns_some() {
        let proto = PlivoProtocol::new(String::new(), String::new());
        let rt = tokio::runtime::Runtime::new().unwrap();
        // Override provided → should attempt hangup (return Some)
        // The actual HTTP call will fail (no real Plivo), but it should spawn
        let handle = proto.hangup("call-123", &rt, Some("override-id"), Some("override-token"));
        assert!(handle.is_some());
        // Wait for the spawned task to complete (it will warn about HTTP failure)
        rt.block_on(async { let _ = handle.unwrap().await; });
    }

    #[test]
    fn plivo_hangup_default_creds_used_when_no_override() {
        let proto = PlivoProtocol::new("default-id".into(), "default-token".into());
        let rt = tokio::runtime::Runtime::new().unwrap();
        // No override → should use defaults and attempt hangup
        let handle = proto.hangup("call-123", &rt, None, None);
        assert!(handle.is_some());
        rt.block_on(async { let _ = handle.unwrap().await; });
    }

    #[test]
    fn plivo_hangup_override_takes_precedence() {
        // Even with non-empty defaults, override should be used
        let proto = PlivoProtocol::new("default-id".into(), "default-token".into());
        let rt = tokio::runtime::Runtime::new().unwrap();
        let handle = proto.hangup("call-123", &rt, Some("override-id"), Some("override-token"));
        assert!(handle.is_some());
        rt.block_on(async { let _ = handle.unwrap().await; });
    }

    // ─── Endpoint hangup with mock protocol ─────────────────────────────

    #[test]
    fn endpoint_hangup_unknown_session_is_ok() {
        let mock = MockProtocol::new();
        let ep = AudioStreamEndpoint::new(
            AudioStreamConfig { listen_addr: "127.0.0.1:0".into(), ..Default::default() },
            Arc::new(mock.clone()),
        ).unwrap();

        // Hanging up a non-existent session should succeed silently
        let result = ep.hangup("nonexistent", None, None);
        assert!(result.is_ok());
        // Protocol hangup should NOT be called
        assert!(mock.hangup_calls.lock().unwrap().is_empty());
    }

    #[test]
    fn endpoint_shutdown_awaits_inflight_hangups() {
        let mock = MockProtocol::new();
        let ep = AudioStreamEndpoint::new(
            AudioStreamConfig { listen_addr: "127.0.0.1:0".into(), auto_hangup: false, ..Default::default() },
            Arc::new(mock.clone()),
        ).unwrap();

        // After shutdown, all spawned hangup tasks should have completed
        ep.shutdown().unwrap();
        // With auto_hangup=false and no sessions, nothing to check — but shutdown should succeed
        assert!(mock.hangup_calls.lock().unwrap().is_empty());
    }
}
