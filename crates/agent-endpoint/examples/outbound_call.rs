//! Outbound call example — calls a destination and prints audio frame stats.
//!
//! Usage:
//!   PLIVO_USER=xxx PLIVO_PASS=yyy cargo run --example outbound_call -- sip:+15551234567@phone.plivo.com
//!
//! For Plivo endpoints, dial format: sip:<number>@phone.plivo.com

use agent_endpoint::{EndpointConfig, EndpointEvent, SipEndpoint};
use std::env;
use std::time::{Duration, Instant};

fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();

    let username = env::var("PLIVO_USER").expect("Set PLIVO_USER env var");
    let password = env::var("PLIVO_PASS").expect("Set PLIVO_PASS env var");
    let sip_server = env::var("PLIVO_SIP_DOMAIN").unwrap_or_else(|_| "phone.plivo.com".into());
    let dest = env::args()
        .nth(1)
        .unwrap_or_else(|| format!("sip:+14157583659@{}", sip_server));

    let config = EndpointConfig {
        sip_server: sip_server.clone(),
        log_level: 4,
        ..Default::default()
    };

    let ep = SipEndpoint::new(config)?;
    ep.register(&username, &password)?;

    // Wait for registration
    let events = ep.events();
    loop {
        match events.recv()? {
            EndpointEvent::Registered => {
                println!("=== Registered ===");
                break;
            }
            EndpointEvent::RegistrationFailed { error } => {
                anyhow::bail!("Registration failed: {}", error);
            }
            _ => {}
        }
    }

    // Make the call
    println!("=== Calling {} ===", dest);
    let call_id = ep.call(&dest, None)?;
    println!("Call initiated (call_id={})", call_id);

    let mut media_active = false;
    let mut frames_received = 0u64;
    let mut last_stats = Instant::now();
    let call_start = Instant::now();

    // Handle call events + poll audio
    loop {
        // Check for events (non-blocking with short timeout)
        match events.recv_timeout(Duration::from_millis(10)) {
            Ok(EndpointEvent::CallStateChanged { session }) => {
                println!("=== Call state: {:?} ===", session.state);
            }
            Ok(EndpointEvent::CallMediaActive { call_id: cid }) => {
                println!("=== Media active on call {} ===", cid);
                media_active = true;
            }
            Ok(EndpointEvent::DtmfReceived { call_id: cid, digit, .. }) => {
                println!("DTMF on call {}: {}", cid, digit);
            }
            Ok(EndpointEvent::CallTerminated { session: _, reason }) => {
                println!("=== Call ended: {} ===", reason);
                break;
            }
            Ok(other) => {
                println!("Event: {:?}", other);
            }
            Err(crossbeam_channel::RecvTimeoutError::Timeout) => {}
            Err(crossbeam_channel::RecvTimeoutError::Disconnected) => break,
        }

        // Poll for received audio frames
        if media_active {
            while let Ok(Some(frame)) = ep.recv_audio(call_id) {
                frames_received += 1;
                if last_stats.elapsed() >= Duration::from_secs(2) {
                    println!(
                        "[{:.1}s] Received {} frames so far | last frame: {} samples @ {}Hz",
                        call_start.elapsed().as_secs_f64(),
                        frames_received,
                        frame.samples_per_channel,
                        frame.sample_rate,
                    );
                    last_stats = Instant::now();
                }
            }
        }

        // Auto-hangup after 15 seconds
        if call_start.elapsed() > Duration::from_secs(15) {
            println!("=== Auto-hanging up after 15s ===");
            ep.hangup(call_id)?;
        }
    }

    println!(
        "Total audio frames received: {} ({:.1}s of audio)",
        frames_received,
        frames_received as f64 * 0.02 // 20ms per frame
    );

    ep.shutdown()?;
    Ok(())
}
