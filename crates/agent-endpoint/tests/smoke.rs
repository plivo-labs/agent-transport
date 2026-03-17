//! Smoke test — verifies PJSUA initializes and shuts down without credentials.

use agent_endpoint::{EndpointConfig, SipEndpoint};

#[test]
fn pjsua_init_and_shutdown() {
    let config = EndpointConfig {
        log_level: 0, // silent
        ..Default::default()
    };

    let ep = SipEndpoint::new(config).expect("PJSUA should initialize");
    assert!(!ep.is_registered());
    ep.shutdown().expect("PJSUA should shut down cleanly");
}

#[test]
fn audio_frame_basics() {
    use agent_endpoint::AudioFrame;

    // 20ms of silence at 48kHz mono
    let frame = AudioFrame::silence(48000, 1, 20);
    assert_eq!(frame.samples_per_channel, 960);
    assert_eq!(frame.duration_ms(), 20);
    assert_eq!(frame.total_samples(), 960);

    // Roundtrip through bytes
    let bytes = frame.as_bytes();
    assert_eq!(bytes.len(), 960 * 2); // 2 bytes per i16
    let restored = AudioFrame::from_bytes(&bytes, 48000, 1);
    assert_eq!(restored.data, frame.data);
}
