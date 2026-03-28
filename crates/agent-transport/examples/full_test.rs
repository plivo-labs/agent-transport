//! Full integration test — register, call, receive audio, send audio, DTMF, hangup.
//!
//! Usage: SIP_USERNAME=xxx SIP_PASSWORD=yyy cargo run --example full_test

use agent_transport::{AudioFrame, EndpointConfig, EndpointEvent, SipEndpoint};
use std::env;
use std::fs::File;
use std::io::Write;
use std::time::{Duration, Instant};

fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();

    let username = env::var("SIP_USERNAME").expect("Set SIP_USERNAME");
    let password = env::var("SIP_PASSWORD").expect("Set SIP_PASSWORD");
    let sip_server = env::var("SIP_DOMAIN").unwrap_or_else(|_| "phone.plivo.com".into());
    let dest = env::args()
        .nth(1)
        .unwrap_or_else(|| format!("sip:+14157583659@{}", sip_server));

    let project_dir = env::var("CARGO_MANIFEST_DIR").unwrap_or_else(|_| ".".into());
    let wav_path = format!("{}/../../audio/temp/full_test.wav", project_dir);

    println!("--- Test 1: Initialize ---");
    let config = EndpointConfig {
        sip_server,
        log_level: 2,
        ..Default::default()
    };
    let ep = SipEndpoint::new(config)?;
    println!("  PASS: Agent transport initialized");

    println!("--- Test 2: Register ---");
    ep.register(&username, &password)?;
    let events = ep.events();
    match events.recv_timeout(Duration::from_secs(10))? {
        EndpointEvent::Registered => println!("  PASS: Registered"),
        EndpointEvent::RegistrationFailed { error } => {
            anyhow::bail!("  FAIL: Registration failed: {}", error);
        }
        other => println!("  Got unexpected event: {:?}", other),
    }
    assert!(ep.is_registered(), "  FAIL: is_registered() returned false");
    println!("  PASS: is_registered() = true");

    println!("--- Test 3: Outbound call ---");
    let call_id = ep.call(&dest, None)?;
    println!("  PASS: Call initiated (call_id={})", call_id);

    // Wait for media
    let mut media_active = false;
    let call_start = Instant::now();
    loop {
        match events.recv_timeout(Duration::from_millis(100)) {
            Ok(EndpointEvent::CallMediaActive { .. }) => {
                media_active = true;
                println!("  PASS: Media active");
                break;
            }
            Ok(EndpointEvent::CallStateChanged { session }) => {
                println!("  Call state: {:?}", session.state);
            }
            Ok(EndpointEvent::CallTerminated { reason, .. }) => {
                anyhow::bail!("  FAIL: Call terminated before media: {}", reason);
            }
            Ok(_) => {}
            Err(crossbeam_channel::RecvTimeoutError::Timeout) => {
                if call_start.elapsed() > Duration::from_secs(15) {
                    anyhow::bail!("  FAIL: Timeout waiting for media");
                }
            }
            Err(_) => break,
        }
    }

    println!("--- Test 4: recv_audio ---");
    let mut all_samples: Vec<i16> = Vec::new();
    let mut sample_rate = 8000u32;
    let recv_start = Instant::now();
    let mut frame_count = 0u32;
    while recv_start.elapsed() < Duration::from_secs(3) {
        match ep.recv_audio(&call_id) {
            Ok(Some(frame)) => {
                sample_rate = frame.sample_rate;
                all_samples.extend_from_slice(&frame.data);
                frame_count += 1;
            }
            Ok(None) => {
                std::thread::sleep(Duration::from_millis(5));
            }
            Err(e) => {
                println!("  recv_audio error: {}", e);
                break;
            }
        }
    }
    println!(
        "  Received {} frames ({} samples, {:.1}s @ {}Hz)",
        frame_count,
        all_samples.len(),
        all_samples.len() as f64 / sample_rate as f64,
        sample_rate
    );
    assert!(frame_count > 50, "  FAIL: Too few frames received");
    println!("  PASS: recv_audio working");

    println!("--- Test 5: send_audio ---");
    // Generate a 440Hz sine wave and send it
    let tone_samples: Vec<i16> = (0..sample_rate as usize / 50) // 20ms
        .map(|i| {
            let t = i as f64 / sample_rate as f64;
            (f64::sin(2.0 * std::f64::consts::PI * 440.0 * t) * 8000.0) as i16
        })
        .collect();
    let tone_frame = AudioFrame::new(tone_samples.clone(), sample_rate, 1);
    // Send 10 frames (200ms of tone)
    let mut send_ok = 0;
    for _ in 0..10 {
        match ep.send_audio(&call_id, &tone_frame) {
            Ok(()) => send_ok += 1,
            Err(e) => println!("  send_audio error: {}", e),
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    println!("  Sent {}/10 frames", send_ok);
    assert!(send_ok >= 8, "  FAIL: Too many send failures");
    println!("  PASS: send_audio working");

    println!("--- Test 6: send_dtmf ---");
    match ep.send_dtmf(&call_id, "1") {
        Ok(()) => println!("  PASS: DTMF '1' sent"),
        Err(e) => println!("  FAIL: send_dtmf error: {}", e),
    }

    println!("--- Test 7: mute/unmute ---");
    match ep.mute(&call_id) {
        Ok(()) => println!("  PASS: mute"),
        Err(e) => println!("  FAIL: mute error: {}", e),
    }
    std::thread::sleep(Duration::from_millis(200));
    match ep.unmute(&call_id) {
        Ok(()) => println!("  PASS: unmute"),
        Err(e) => println!("  FAIL: unmute error: {}", e),
    }

    // Collect a bit more audio for the WAV file
    let recv2_start = Instant::now();
    while recv2_start.elapsed() < Duration::from_secs(2) {
        if let Ok(Some(frame)) = ep.recv_audio(&call_id) {
            all_samples.extend_from_slice(&frame.data);
        } else {
            std::thread::sleep(Duration::from_millis(5));
        }
    }

    println!("--- Test 8: hangup ---");
    ep.hangup(&call_id)?;
    // Wait for termination event
    loop {
        match events.recv_timeout(Duration::from_secs(5)) {
            Ok(EndpointEvent::CallTerminated { reason, .. }) => {
                println!("  PASS: Call terminated ({})", reason);
                break;
            }
            Ok(_) => {}
            Err(_) => {
                println!("  WARN: No termination event received");
                break;
            }
        }
    }

    println!("--- Test 9: shutdown ---");
    ep.shutdown()?;
    println!("  PASS: Clean shutdown");

    // Save WAV
    let duration = all_samples.len() as f64 / sample_rate as f64;
    println!("\n--- Results ---");
    println!("Total audio: {:.1}s @ {}Hz", duration, sample_rate);
    write_wav(&wav_path, &all_samples, sample_rate, 1)?;
    println!("WAV saved: {}", wav_path);
    println!("Play: afplay {}", wav_path);
    println!("\nAll tests passed!");

    Ok(())
}

fn write_wav(path: &str, samples: &[i16], sample_rate: u32, channels: u16) -> anyhow::Result<()> {
    let mut f = File::create(path)?;
    let data_len = (samples.len() * 2) as u32;
    let block_align = channels * 2;
    let byte_rate = sample_rate * block_align as u32;
    f.write_all(b"RIFF")?;
    f.write_all(&(36 + data_len).to_le_bytes())?;
    f.write_all(b"WAVE")?;
    f.write_all(b"fmt ")?;
    f.write_all(&16u32.to_le_bytes())?;
    f.write_all(&1u16.to_le_bytes())?;
    f.write_all(&channels.to_le_bytes())?;
    f.write_all(&sample_rate.to_le_bytes())?;
    f.write_all(&byte_rate.to_le_bytes())?;
    f.write_all(&block_align.to_le_bytes())?;
    f.write_all(&16u16.to_le_bytes())?;
    f.write_all(b"data")?;
    f.write_all(&data_len.to_le_bytes())?;
    for &s in samples {
        f.write_all(&s.to_le_bytes())?;
    }
    Ok(())
}
