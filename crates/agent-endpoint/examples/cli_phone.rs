//! CLI phone — make a real call from your terminal and talk.
//!
//! Uses your system microphone and speaker. Press Enter to hang up.
//!
//! Usage:
//!   PLIVO_USER=xxx PLIVO_PASS=yyy cargo run --example cli_phone -- sip:+15551234567@phone.plivo.com
//!
//! You can also receive calls — if no destination is given, it waits for inbound calls.

use agent_endpoint::{EndpointConfig, EndpointEvent, SipEndpoint};
use std::env;
use std::io::{self, BufRead};
use std::time::Duration;

fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();

    let username = env::var("PLIVO_USER").expect("Set PLIVO_USER env var");
    let password = env::var("PLIVO_PASS").expect("Set PLIVO_PASS env var");
    let sip_server = env::var("PLIVO_SIP_DOMAIN").unwrap_or_else(|_| "phone.plivo.com".into());
    let dest = env::args().nth(1);

    let config = EndpointConfig {
        sip_server: sip_server.clone(),
        log_level: 3,
        use_sound_device: true,
        ..Default::default()
    };

    println!("Initializing SIP endpoint...");
    let ep = SipEndpoint::new(config)?;

    println!("Registering as {}@{}...", username, sip_server);
    ep.register(&username, &password)?;

    let events = ep.events();

    // Wait for registration
    loop {
        match events.recv_timeout(Duration::from_secs(10))? {
            EndpointEvent::Registered => {
                println!("Registered successfully.");
                break;
            }
            EndpointEvent::RegistrationFailed { error } => {
                anyhow::bail!("Registration failed: {}", error);
            }
            _ => {}
        }
    }

    let call_id = if let Some(ref dest_uri) = dest {
        // Outbound call
        println!("Calling {}...", dest_uri);
        let id = ep.call(dest_uri, None)?;
        println!("Call initiated (call_id={}). Waiting for answer...", id);
        id
    } else {
        // Wait for inbound call
        println!("Waiting for incoming call... (Ctrl+C to quit)");
        loop {
            match events.recv()? {
                EndpointEvent::IncomingCall { session } => {
                    println!(
                        "Incoming call from {} (call_id={})",
                        session.remote_uri, session.call_id
                    );
                    println!("Answering...");
                    ep.answer(session.call_id, 200)?;
                    break session.call_id;
                }
                _ => {}
            }
        }
    };

    // Event loop — print status, wait for media, handle termination
    let mut connected = false;
    let stdin = io::stdin();

    // Spawn a thread to watch for Enter key
    let (hangup_tx, hangup_rx) = crossbeam_channel::bounded::<()>(1);
    std::thread::spawn(move || {
        let mut line = String::new();
        let _ = stdin.lock().read_line(&mut line);
        let _ = hangup_tx.send(());
    });

    loop {
        // Check for hangup signal (Enter pressed)
        if hangup_rx.try_recv().is_ok() {
            println!("Hanging up...");
            ep.hangup(call_id)?;
        }

        match events.recv_timeout(Duration::from_millis(100)) {
            Ok(EndpointEvent::CallStateChanged { session }) => {
                println!("Call state: {:?}", session.state);
            }
            Ok(EndpointEvent::CallMediaActive { .. }) => {
                if !connected {
                    connected = true;
                    println!();
                    println!("=== CONNECTED — you can talk now ===");
                    println!("Press Enter to hang up.");
                    println!();
                }
            }
            Ok(EndpointEvent::CallTerminated { reason, .. }) => {
                println!("Call ended: {}", reason);
                break;
            }
            Ok(EndpointEvent::DtmfReceived { digit, .. }) => {
                println!("DTMF: {}", digit);
            }
            Ok(_) => {}
            Err(crossbeam_channel::RecvTimeoutError::Timeout) => {}
            Err(crossbeam_channel::RecvTimeoutError::Disconnected) => break,
        }
    }

    ep.shutdown()?;
    println!("Done.");
    Ok(())
}
