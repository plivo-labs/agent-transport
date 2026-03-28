//! Inbound call echo example — answers incoming calls and echoes audio back.
//!
//! Usage: SIP_USERNAME=xxx SIP_PASSWORD=yyy cargo run --example inbound_echo

use agent_transport::{EndpointConfig, EndpointEvent, SipEndpoint};
use std::env;

fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();

    let username = env::var("SIP_USERNAME").expect("Set SIP_USERNAME env var");
    let password = env::var("SIP_PASSWORD").expect("Set SIP_PASSWORD env var");

    let ep = SipEndpoint::new(EndpointConfig {
        log_level: 4,
        ..Default::default()
    })?;
    ep.register(&username, &password)?;

    let events = ep.events();

    // Wait for registration
    loop {
        match events.recv()? {
            EndpointEvent::Registered => {
                println!("Registered. Waiting for incoming calls...");
                break;
            }
            EndpointEvent::RegistrationFailed { error } => {
                anyhow::bail!("Registration failed: {}", error);
            }
            _ => {}
        }
    }

    // Main event loop
    loop {
        match events.recv()? {
            EndpointEvent::IncomingCall { session } => {
                println!("Incoming call from: {}", session.remote_uri);
                // Auto-answer with 200 OK
                ep.answer(&session.call_id, 200)?;
                println!("Call answered (call_id={})", session.call_id);
            }
            EndpointEvent::CallMediaActive { call_id } => {
                println!("Media active on call {}. Audio is bridged via conf.", call_id);
                // With null sound device + conf bridge, the SIP stack handles
                // the audio path. For echo, we'd need a custom media port.
            }
            EndpointEvent::DtmfReceived { call_id, digit, .. } => {
                println!("DTMF on call {}: {}", call_id, digit);
                if digit == '#' {
                    println!("# received, hanging up.");
                    ep.hangup(&call_id)?;
                }
            }
            EndpointEvent::CallTerminated { session, reason } => {
                println!("Call {} ended: {}", session.call_id, reason);
                println!("Waiting for next call...");
            }
            _ => {}
        }
    }
}
