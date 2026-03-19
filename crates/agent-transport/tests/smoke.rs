//! Smoke test — verifies endpoint initializes and shuts down.

use agent_transport::{EndpointConfig, SipEndpoint};

#[test]
fn endpoint_init_and_shutdown() {
    let config = EndpointConfig {
        log_level: 0,
        ..Default::default()
    };

    let ep = SipEndpoint::new(config).expect("SipEndpoint should initialize");
    assert!(!ep.is_registered());
    ep.shutdown().expect("SipEndpoint should shut down cleanly");
}

#[test]
fn audio_frame_basics() {
    use agent_transport::AudioFrame;

    // 20ms of silence at 48kHz mono
    let frame = AudioFrame::silence(48000, 1, 20);
    assert_eq!(frame.samples_per_channel, 960);
    assert_eq!(frame.duration_ms(), 20);
    assert_eq!(frame.total_samples(), 960);

    // Roundtrip through bytes
    let bytes = frame.as_bytes();
    assert_eq!(bytes.len(), 960 * 2);
    let restored = AudioFrame::from_bytes(&bytes, 48000, 1);
    assert_eq!(restored.data, frame.data);
}
