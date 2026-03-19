//! RTP transport — sends and receives audio over UDP with G.711 codec.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU16, AtomicU32, Ordering};
use std::sync::Arc;
use std::time::Duration;

use bytes::{Bytes, BytesMut};
use crossbeam_channel::{Receiver, Sender};
use rtc_rtp::header::Header;
use rtc_rtp::packet::Packet;
use rtc_shared::marshal::{Marshal, Unmarshal};
use tokio::net::UdpSocket;
use tokio_util::sync::CancellationToken;
use tracing::{debug, warn};

use beep_detector::{BeepDetector, BeepDetectorResult};

use crate::audio::AudioFrame;
use crate::config::Codec;
use crate::dtmf;
use crate::events::EndpointEvent;

/// DTMF telephone-event payload type (standard dynamic PT).
pub(crate) const DTMF_PAYLOAD_TYPE: u8 = 101;

/// RTP transport for a single call.
pub(crate) struct RtpTransport {
    pub socket: Arc<UdpSocket>,
    pub remote_addr: SocketAddr,
    pub ssrc: u32,
    pub codec: Codec,
    pub seq: AtomicU16,
    pub timestamp: AtomicU32,
    pub cancel: CancellationToken,
}

impl RtpTransport {
    pub fn new(
        socket: Arc<UdpSocket>,
        remote_addr: SocketAddr,
        codec: Codec,
        cancel: CancellationToken,
    ) -> Self {
        Self {
            socket,
            remote_addr,
            ssrc: rand::random(),
            codec,
            seq: AtomicU16::new(0),
            timestamp: AtomicU32::new(0),
            cancel,
        }
    }

    /// Send one RTP packet with the given payload.
    pub async fn send_rtp(&self, payload: &[u8], marker: bool) -> std::io::Result<()> {
        let seq = self.seq.fetch_add(1, Ordering::Relaxed);
        let ts = self.timestamp.load(Ordering::Relaxed);

        let packet = Packet {
            header: Header {
                version: 2,
                marker,
                payload_type: self.codec.payload_type(),
                sequence_number: seq,
                timestamp: ts,
                ssrc: self.ssrc,
                ..Default::default()
            },
            payload: Bytes::copy_from_slice(payload),
        };

        let buf = packet.marshal().map_err(|e| {
            std::io::Error::new(std::io::ErrorKind::Other, format!("RTP marshal: {}", e))
        })?;
        self.socket.send_to(&buf, self.remote_addr).await?;
        Ok(())
    }

    /// Send an RFC 4733 DTMF event.
    pub async fn send_dtmf_event(
        &self,
        digit: char,
        duration_samples: u16,
    ) -> std::io::Result<()> {
        let event_code = dtmf::digit_to_event(digit).unwrap_or(0);
        let ts = self.timestamp.load(Ordering::Relaxed);

        // Send start event
        let payload = dtmf::encode_rfc4733(event_code, false, 10, 0);
        self.send_rtp_raw(DTMF_PAYLOAD_TYPE, ts, true, &payload)
            .await?;

        // Send end event (3 times for reliability per RFC 4733)
        let end_payload = dtmf::encode_rfc4733(event_code, true, 10, duration_samples);
        for _ in 0..3 {
            self.send_rtp_raw(DTMF_PAYLOAD_TYPE, ts, false, &end_payload)
                .await?;
        }
        Ok(())
    }

    async fn send_rtp_raw(
        &self,
        pt: u8,
        timestamp: u32,
        marker: bool,
        payload: &[u8],
    ) -> std::io::Result<()> {
        let seq = self.seq.fetch_add(1, Ordering::Relaxed);
        let packet = Packet {
            header: Header {
                version: 2,
                marker,
                payload_type: pt,
                sequence_number: seq,
                timestamp,
                ssrc: self.ssrc,
                ..Default::default()
            },
            payload: Bytes::copy_from_slice(payload),
        };
        let buf = packet.marshal().map_err(|e| {
            std::io::Error::new(std::io::ErrorKind::Other, format!("RTP marshal: {}", e))
        })?;
        self.socket.send_to(&buf, self.remote_addr).await?;
        Ok(())
    }

    /// Start the RTP send loop (reads 16kHz frames, resamples, encodes, sends).
    pub fn start_send_loop(
        self: &Arc<Self>,
        outgoing_rx: Receiver<Vec<i16>>,
        muted: Arc<AtomicBool>,
        paused: Arc<AtomicBool>,
        flush_flag: Arc<AtomicBool>,
        playout_notify: Arc<(std::sync::Mutex<bool>, std::sync::Condvar)>,
    ) -> tokio::task::JoinHandle<()> {
        let transport = Arc::clone(self);
        let cancel = self.cancel.clone();

        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_millis(20));
            let samples_per_frame = 160u32; // 20ms at 8kHz

            loop {
                tokio::select! {
                    _ = cancel.cancelled() => break,
                    _ = interval.tick() => {}
                }

                // Clear buffer requested (barge-in)
                if flush_flag.swap(false, Ordering::Relaxed) {
                    while outgoing_rx.try_recv().is_ok() {}
                    notify_playout(&playout_notify);
                    continue;
                }

                // Paused — send silence, keep queue
                if paused.load(Ordering::Relaxed) {
                    let silence = vec![0u8; samples_per_frame as usize];
                    let _ = transport.send_rtp(&silence, false).await;
                    transport.timestamp.fetch_add(samples_per_frame, Ordering::Relaxed);
                    continue;
                }

                match outgoing_rx.try_recv() {
                    Ok(samples_16k) => {
                        if muted.load(Ordering::Relaxed) {
                            // Muted — consume frame but send silence
                            let silence = vec![0u8; samples_per_frame as usize];
                            let _ = transport.send_rtp(&silence, false).await;
                        } else {
                            // Resample 16kHz → 8kHz, encode G.711
                            let samples_8k = resample_16k_to_8k(&samples_16k);
                            let encoded = g711_encode(&samples_8k, transport.codec);
                            let _ = transport.send_rtp(&encoded, false).await;
                        }
                        transport.timestamp.fetch_add(samples_per_frame, Ordering::Relaxed);
                    }
                    Err(_) => {
                        // Queue empty — send silence, notify playout
                        let silence = vec![0u8; samples_per_frame as usize];
                        let _ = transport.send_rtp(&silence, false).await;
                        transport.timestamp.fetch_add(samples_per_frame, Ordering::Relaxed);
                        notify_playout(&playout_notify);
                    }
                }
            }
        })
    }

    /// Start the RTP recv loop (receives, decodes, pushes AudioFrame).
    pub fn start_recv_loop(
        self: &Arc<Self>,
        incoming_tx: Sender<AudioFrame>,
        event_tx: Sender<EndpointEvent>,
        call_id: i32,
        beep_detector: Arc<std::sync::Mutex<Option<BeepDetector>>>,
    ) -> tokio::task::JoinHandle<()> {
        let transport = Arc::clone(self);
        let cancel = self.cancel.clone();

        tokio::spawn(async move {
            let mut buf = vec![0u8; 2048];

            loop {
                tokio::select! {
                    _ = cancel.cancelled() => break,
                    result = transport.socket.recv_from(&mut buf) => {
                        match result {
                            Ok((len, _addr)) => {
                                let mut data = &buf[..len];
                                let packet = match Packet::unmarshal(&mut data) {
                                    Ok(p) => p,
                                    Err(_) => continue,
                                };

                                // DTMF telephone-event
                                if packet.header.payload_type == DTMF_PAYLOAD_TYPE {
                                    if let Some((event, is_end, _, _)) =
                                        dtmf::decode_rfc4733(&packet.payload)
                                    {
                                        if is_end {
                                            if let Some(digit) = dtmf::event_to_digit(event) {
                                                debug!("DTMF received: {}", digit);
                                                let _ = event_tx.try_send(
                                                    EndpointEvent::DtmfReceived {
                                                        call_id,
                                                        digit,
                                                        method: "rfc2833".into(),
                                                    },
                                                );
                                            }
                                        }
                                    }
                                    continue;
                                }

                                // Decode G.711 → PCM 8kHz → resample to 16kHz
                                let samples_8k = g711_decode(&packet.payload, transport.codec);
                                let samples_16k = resample_8k_to_16k(&samples_8k);

                                // Feed beep detector
                                if let Ok(mut guard) = beep_detector.lock() {
                                    if let Some(ref mut detector) = *guard {
                                        match detector.process_frame(&samples_16k) {
                                            BeepDetectorResult::Detected(event) => {
                                                let _ = event_tx.try_send(
                                                    EndpointEvent::BeepDetected {
                                                        call_id,
                                                        frequency_hz: event.frequency_hz,
                                                        duration_ms: event.duration_ms,
                                                    },
                                                );
                                                *guard = None; // auto-remove
                                            }
                                            BeepDetectorResult::Timeout => {
                                                let _ = event_tx.try_send(
                                                    EndpointEvent::BeepTimeout { call_id },
                                                );
                                                *guard = None;
                                            }
                                            BeepDetectorResult::Listening => {}
                                        }
                                    }
                                }

                                let frame = AudioFrame {
                                    data: samples_16k,
                                    sample_rate: 16000,
                                    num_channels: 1,
                                    samples_per_channel: 320, // 20ms at 16kHz
                                };
                                let _ = incoming_tx.try_send(frame);
                            }
                            Err(e) => {
                                warn!("RTP recv error: {}", e);
                            }
                        }
                    }
                }
            }
        })
    }
}

fn notify_playout(playout_notify: &Arc<(std::sync::Mutex<bool>, std::sync::Condvar)>) {
    if let Ok(mut done) = playout_notify.0.lock() {
        *done = true;
        playout_notify.1.notify_all();
    }
}

// ─── G.711 Codec ─────────────────────────────────────────────────────────────

/// Encode PCM samples to G.711 bytes.
pub(crate) fn g711_encode(samples: &[i16], codec: Codec) -> Vec<u8> {
    match codec {
        Codec::PCMU => samples.iter().map(|&s| linear_to_ulaw(s)).collect(),
        Codec::PCMA => samples.iter().map(|&s| linear_to_alaw(s)).collect(),
        _ => samples.iter().map(|&s| linear_to_ulaw(s)).collect(), // fallback to PCMU
    }
}

/// Decode G.711 bytes to PCM samples.
pub(crate) fn g711_decode(bytes: &[u8], codec: Codec) -> Vec<i16> {
    match codec {
        Codec::PCMU => bytes.iter().map(|&b| ulaw_to_linear(b)).collect(),
        Codec::PCMA => bytes.iter().map(|&b| alaw_to_linear(b)).collect(),
        _ => bytes.iter().map(|&b| ulaw_to_linear(b)).collect(),
    }
}

// ITU-T G.711 mu-law encode
fn linear_to_ulaw(mut sample: i16) -> u8 {
    const BIAS: i16 = 0x84;
    const CLIP: i16 = 32635;

    let sign = if sample < 0 {
        sample = -sample;
        0x80u8
    } else {
        0x00u8
    };

    if sample > CLIP {
        sample = CLIP;
    }
    sample += BIAS;

    let mut exponent = 7u8;
    let mut mask = 0x4000i16;
    while (sample & mask) == 0 && exponent > 0 {
        exponent -= 1;
        mask >>= 1;
    }

    let mantissa = ((sample >> (exponent + 3)) & 0x0F) as u8;
    !(sign | (exponent << 4) | mantissa)
}

// ITU-T G.711 mu-law decode
fn ulaw_to_linear(mut byte: u8) -> i16 {
    byte = !byte;
    let sign = byte & 0x80;
    let exponent = ((byte >> 4) & 0x07) as i16;
    let mantissa = (byte & 0x0F) as i16;

    let mut sample = ((mantissa << 1) | 0x21) << (exponent + 2);
    sample -= 0x84;

    if sign != 0 {
        -sample
    } else {
        sample
    }
}

// ITU-T G.711 A-law encode
fn linear_to_alaw(mut sample: i16) -> u8 {
    let sign = if sample >= 0 {
        0x55u8
    } else {
        sample = -sample - 1;
        0xD5u8
    };

    if sample > 32767 {
        sample = 32767;
    }

    let mut exponent = 7u8;
    let mut mask = 0x4000i16;
    while (sample & mask) == 0 && exponent > 0 {
        exponent -= 1;
        mask >>= 1;
    }

    let mantissa = if exponent > 0 {
        ((sample >> (exponent + 3)) & 0x0F) as u8
    } else {
        ((sample >> 4) & 0x0F) as u8
    };

    (sign ^ ((exponent << 4) | mantissa)) & 0xFF
}

// ITU-T G.711 A-law decode
fn alaw_to_linear(byte: u8) -> i16 {
    let byte = byte ^ 0x55;
    let sign = byte & 0x80;
    let exponent = ((byte >> 4) & 0x07) as i16;
    let mantissa = (byte & 0x0F) as i16;

    let mut sample = if exponent == 0 {
        (mantissa << 4) | 0x08
    } else {
        ((mantissa << 1) | 0x21) << (exponent + 2)
    };

    if sign != 0 {
        sample = -sample;
    }
    sample
}

// ─── Resampling ──────────────────────────────────────────────────────────────

/// Resample from 8kHz to 16kHz (linear interpolation).
pub(crate) fn resample_8k_to_16k(samples: &[i16]) -> Vec<i16> {
    let mut out = Vec::with_capacity(samples.len() * 2);
    for i in 0..samples.len() {
        out.push(samples[i]);
        // Interpolate between current and next sample
        let next = if i + 1 < samples.len() {
            samples[i + 1]
        } else {
            samples[i]
        };
        out.push(((samples[i] as i32 + next as i32) / 2) as i16);
    }
    out
}

/// Resample from 16kHz to 8kHz (take every other sample).
pub(crate) fn resample_16k_to_8k(samples: &[i16]) -> Vec<i16> {
    samples.iter().step_by(2).copied().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_pcmu_roundtrip() {
        // Test a range of samples
        for &sample in &[0i16, 100, 1000, 8000, 16000, -100, -1000, -8000, -16000] {
            let encoded = linear_to_ulaw(sample);
            let decoded = ulaw_to_linear(encoded);
            // G.711 is lossy, but should be within ~1% for large values
            let diff = (sample as i32 - decoded as i32).unsigned_abs();
            assert!(
                diff < (sample.unsigned_abs() as u32 / 10).max(100),
                "PCMU roundtrip: {} -> {} -> {} (diff {})",
                sample,
                encoded,
                decoded,
                diff
            );
        }
    }

    #[test]
    fn test_pcma_roundtrip() {
        for &sample in &[0i16, 100, 1000, 8000, 16000, -100, -1000, -8000, -16000] {
            let encoded = linear_to_alaw(sample);
            let decoded = alaw_to_linear(encoded);
            let diff = (sample as i32 - decoded as i32).unsigned_abs();
            assert!(
                diff < (sample.unsigned_abs() as u32 / 10).max(100),
                "PCMA roundtrip: {} -> {} -> {} (diff {})",
                sample,
                encoded,
                decoded,
                diff
            );
        }
    }

    #[test]
    fn test_resample_8k_to_16k() {
        let input = vec![0i16, 100, 200, 300];
        let output = resample_8k_to_16k(&input);
        assert_eq!(output.len(), 8); // doubles
        assert_eq!(output[0], 0);
        assert_eq!(output[2], 100);
        assert_eq!(output[4], 200);
    }

    #[test]
    fn test_resample_16k_to_8k() {
        let input = vec![0i16, 50, 100, 150, 200, 250, 300, 350];
        let output = resample_16k_to_8k(&input);
        assert_eq!(output.len(), 4); // halves
        assert_eq!(output, vec![0, 100, 200, 300]);
    }

    #[test]
    fn test_g711_encode_decode_pcmu() {
        let samples = vec![0i16, 1000, -1000, 8000, -8000];
        let encoded = g711_encode(&samples, Codec::PCMU);
        assert_eq!(encoded.len(), 5);
        let decoded = g711_decode(&encoded, Codec::PCMU);
        assert_eq!(decoded.len(), 5);
    }
}
