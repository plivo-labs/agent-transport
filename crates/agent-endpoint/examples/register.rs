//! Basic registration example.
//!
//! Usage: SIP_USERNAME=xxx SIP_PASSWORD=yyy cargo run --example register

use agent_endpoint::{EndpointConfig, EndpointEvent, SipEndpoint};
use std::env;

fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();

    let username = env::var("SIP_USERNAME").expect("Set SIP_USERNAME env var");
    let password = env::var("SIP_PASSWORD").expect("Set SIP_PASSWORD env var");

    let sip_server = env::var("SIP_DOMAIN").unwrap_or_else(|_| "phone.plivo.com".into());

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
