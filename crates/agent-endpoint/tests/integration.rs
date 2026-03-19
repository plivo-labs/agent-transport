//! Integration tests — require a live SIP account.
//!
//! Run: SIP_USERNAME=xxx SIP_PASSWORD=yyy cargo test -p agent-endpoint --features integration
//!
//! These tests register with a SIP server, make real calls, and verify the full
//! call lifecycle including audio I/O.
#![cfg(feature = "integration")]

use agent_endpoint::{AudioFrame, EndpointConfig, EndpointEvent, SipEndpoint};
use std::env;
use std::time::Duration;

fn sip_creds() -> (String, String) {
    let user = env::var("SIP_USERNAME").expect("Set SIP_USERNAME env var");
    let pass = env::var("SIP_PASSWORD").expect("Set SIP_PASSWORD env var");
    (user, pass)
}

fn sip_domain() -> String {
    env::var("SIP_DOMAIN").unwrap_or_else(|_| "phone.plivo.com".into())
}

fn dest_uri() -> String {
    env::var("SIP_DEST_URI").unwrap_or_else(|_| {
        format!("sip:+14157583659@{}", sip_domain())
    })
}

/// Create a configured endpoint with low log level for tests.
fn make_endpoint() -> SipEndpoint {
    let config = EndpointConfig {
        sip_server: sip_domain(),
        log_level: 0,
        ..Default::default()
    };
    SipEndpoint::new(config).expect("SipEndpoint::new should succeed")
}

#[test]
fn register_and_unregister() {
    let (user, pass) = sip_creds();
    let ep = make_endpoint();

    ep.register(&user, &pass).expect("register should succeed");

    let events = ep.events();
    let event = events
        .recv_timeout(Duration::from_secs(10))
        .expect("should receive registration event");
    assert!(
        matches!(event, EndpointEvent::Registered),
        "expected Registered, got: {:?}",
        event
    );
    assert!(ep.is_registered());

    ep.unregister().expect("unregister should succeed");
    ep.shutdown().expect("shutdown should succeed");
}

#[test]
fn outbound_call_and_hangup() {
    let (user, pass) = sip_creds();
    let ep = make_endpoint();

    ep.register(&user, &pass).unwrap();
    let events = ep.events();

    // Wait for registration
    loop {
        match events.recv_timeout(Duration::from_secs(10)).unwrap() {
            EndpointEvent::Registered => break,
            _ => continue,
        }
    }

    // Make call
    let call_id = ep.call(&dest_uri(), None).expect("call should succeed");
    assert!(call_id >= 0);

    // Wait for media active (call answered)
    let start = std::time::Instant::now();
    let mut media_active = false;
    loop {
        match events.recv_timeout(Duration::from_millis(200)) {
            Ok(EndpointEvent::CallMediaActive { .. }) => {
                media_active = true;
                break;
            }
            Ok(EndpointEvent::CallTerminated { reason, .. }) => {
                panic!("Call terminated before media: {}", reason);
            }
            Ok(_) => {}
            Err(_) => {
                if start.elapsed() > Duration::from_secs(15) {
                    panic!("Timeout waiting for media");
                }
            }
        }
    }
    assert!(media_active);

    // Hangup
    ep.hangup(call_id).expect("hangup should succeed");

    // Wait for termination
    loop {
        match events.recv_timeout(Duration::from_secs(5)) {
            Ok(EndpointEvent::CallTerminated { .. }) => break,
            Ok(_) => {}
            Err(_) => break,
        }
    }

    ep.shutdown().unwrap();
}

#[test]
fn send_and_receive_audio() {
    let (user, pass) = sip_creds();
    let ep = make_endpoint();

    ep.register(&user, &pass).unwrap();
    let events = ep.events();

    // Wait for registration
    loop {
        match events.recv_timeout(Duration::from_secs(10)).unwrap() {
            EndpointEvent::Registered => break,
            _ => continue,
        }
    }

    let call_id = ep.call(&dest_uri(), None).unwrap();

    // Wait for media
    let start = std::time::Instant::now();
    loop {
        match events.recv_timeout(Duration::from_millis(200)) {
            Ok(EndpointEvent::CallMediaActive { .. }) => break,
            Ok(EndpointEvent::CallTerminated { reason, .. }) => {
                panic!("Call terminated before media: {}", reason);
            }
            Ok(_) => {}
            Err(_) => {
                if start.elapsed() > Duration::from_secs(15) {
                    panic!("Timeout waiting for media");
                }
            }
        }
    }

    // Send 10 frames of 440Hz tone
    let sample_rate = 16000u32;
    let tone_samples: Vec<i16> = (0..sample_rate as usize / 50)
        .map(|i| {
            let t = i as f64 / sample_rate as f64;
            (f64::sin(2.0 * std::f64::consts::PI * 440.0 * t) * 8000.0) as i16
        })
        .collect();
    let tone_frame = AudioFrame::new(tone_samples, sample_rate, 1);

    let mut send_ok = 0;
    for _ in 0..10 {
        if ep.send_audio(call_id, &tone_frame).is_ok() {
            send_ok += 1;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(send_ok >= 8, "should send at least 8/10 frames");

    // Receive audio for 1 second
    let mut recv_count = 0;
    let recv_start = std::time::Instant::now();
    while recv_start.elapsed() < Duration::from_secs(1) {
        match ep.recv_audio(call_id) {
            Ok(Some(_)) => recv_count += 1,
            _ => std::thread::sleep(Duration::from_millis(5)),
        }
    }
    assert!(recv_count > 10, "should receive audio frames, got {}", recv_count);

    ep.hangup(call_id).unwrap();
    ep.shutdown().unwrap();
}
