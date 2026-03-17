//! Basic registration example.
//!
//! Usage: PLIVO_USER=xxx PLIVO_PASS=yyy cargo run --example register

use agent_endpoint::{EndpointConfig, EndpointEvent, SipEndpoint};
use std::env;

fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();

    let username = env::var("PLIVO_USER").expect("Set PLIVO_USER env var");
    let password = env::var("PLIVO_PASS").expect("Set PLIVO_PASS env var");

    let sip_server = env::var("PLIVO_SIP_DOMAIN").unwrap_or_else(|_| "phone.plivo.com".into());

    let config = EndpointConfig {
        sip_server,
        log_level: 4,
        ..Default::default()
    };

    let ep = SipEndpoint::new(config)?;
    ep.register(&username, &password)?;

    println!("Registering... (Ctrl+C to quit)");

    // Wait for events
    let events = ep.events();
    loop {
        match events.recv() {
            Ok(EndpointEvent::Registered) => {
                println!("Registered successfully!");
            }
            Ok(EndpointEvent::RegistrationFailed { error }) => {
                println!("Registration failed: {}", error);
                break;
            }
            Ok(event) => {
                println!("Event: {:?}", event);
            }
            Err(_) => break,
        }
    }

    ep.shutdown()?;
    Ok(())
}
