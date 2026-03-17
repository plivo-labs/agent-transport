//! Makes a call and saves received audio to a WAV file so you can listen to it.
//!
//! Usage:
//!   PLIVO_USER=xxx PLIVO_PASS=yyy cargo run --example call_and_record -- sip:+14157583659@phone.plivo.com

use agent_endpoint::{AudioFrame, EndpointConfig, EndpointEvent, SipEndpoint};
use std::env;
use std::fs::File;
use std::io::Write;
use std::time::{Duration, Instant};

fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();

    let username = env::var("PLIVO_USER").expect("Set PLIVO_USER");
    let password = env::var("PLIVO_PASS").expect("Set PLIVO_PASS");
    let sip_server = env::var("PLIVO_SIP_DOMAIN").unwrap_or_else(|_| "phone.plivo.com".into());
    let dest = env::args()
        .nth(1)
        .unwrap_or_else(|| format!("sip:+14157583659@{}", sip_server));
    let project_dir = env::var("CARGO_MANIFEST_DIR")
        .unwrap_or_else(|_| ".".into());
    let default_output = format!("{}/../../audio/temp/plivo_call.wav", project_dir);
    let output_path = env::args().nth(2).unwrap_or(default_output);

    let config = EndpointConfig {
        sip_server,
        log_level: 3,
        ..Default::default()
    };

    let ep = SipEndpoint::new(config)?;
    ep.register(&username, &password)?;

    let events = ep.events();
    loop {
        match events.recv()? {
            EndpointEvent::Registered => {
                println!("Registered.");
                break;
            }
            EndpointEvent::RegistrationFailed { error } => {
                anyhow::bail!("Registration failed: {}", error);
            }
            _ => {}
        }
    }

    println!("Calling {}...", dest);
    let call_id = ep.call(&dest, None)?;

    let mut media_active = false;
    let mut all_samples: Vec<i16> = Vec::new();
    let mut sample_rate = 48000u32;
    let mut num_channels = 1u32;
    let call_start = Instant::now();

    loop {
        match events.recv_timeout(Duration::from_millis(10)) {
            Ok(EndpointEvent::CallMediaActive { .. }) => {
                println!("Media active.");
                media_active = true;
            }
            Ok(EndpointEvent::CallTerminated { reason, .. }) => {
                println!("Call ended: {}", reason);
                break;
            }
            Ok(_) => {}
            Err(crossbeam_channel::RecvTimeoutError::Timeout) => {}
            Err(_) => break,
        }

        if media_active {
            while let Ok(Some(frame)) = ep.recv_audio(call_id) {
                sample_rate = frame.sample_rate;
                num_channels = frame.num_channels;
                all_samples.extend_from_slice(&frame.data);
            }
        }

        if call_start.elapsed() > Duration::from_secs(12) {
            println!("Hanging up after 12s.");
            ep.hangup(call_id)?;
        }
    }

    ep.shutdown()?;

    // Write WAV file
    let duration_secs = all_samples.len() as f64 / sample_rate as f64 / num_channels as f64;
    println!(
        "Captured {} samples ({:.1}s) @ {}Hz {}ch",
        all_samples.len(),
        duration_secs,
        sample_rate,
        num_channels
    );

    write_wav(&output_path, &all_samples, sample_rate, num_channels as u16)?;
    println!("Saved to {}", output_path);
    println!("Play it:  afplay {}", output_path);

    Ok(())
}

fn write_wav(path: &str, samples: &[i16], sample_rate: u32, channels: u16) -> anyhow::Result<()> {
    let mut f = File::create(path)?;
    let data_len = (samples.len() * 2) as u32;
    let bits_per_sample: u16 = 16;
    let block_align = channels * (bits_per_sample / 8);
    let byte_rate = sample_rate * block_align as u32;

    // RIFF header
    f.write_all(b"RIFF")?;
    f.write_all(&(36 + data_len).to_le_bytes())?;
    f.write_all(b"WAVE")?;

    // fmt chunk
    f.write_all(b"fmt ")?;
    f.write_all(&16u32.to_le_bytes())?; // chunk size
    f.write_all(&1u16.to_le_bytes())?; // PCM format
    f.write_all(&channels.to_le_bytes())?;
    f.write_all(&sample_rate.to_le_bytes())?;
    f.write_all(&byte_rate.to_le_bytes())?;
    f.write_all(&block_align.to_le_bytes())?;
    f.write_all(&bits_per_sample.to_le_bytes())?;

    // data chunk
    f.write_all(b"data")?;
    f.write_all(&data_len.to_le_bytes())?;
    for &s in samples {
        f.write_all(&s.to_le_bytes())?;
    }

    Ok(())
}
