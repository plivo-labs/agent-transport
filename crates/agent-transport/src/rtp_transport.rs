//! RTP transport — sends and receives audio over UDP with G.711 codec.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU16, AtomicU32, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use crossbeam_channel::{Receiver, Sender};
use rtp::packet::Packet;
use rtp::header::Header;
use tokio::net::UdpSocket;
use tokio_util::sync::CancellationToken;
use tracing::{debug, warn};
use webrtc_util::marshal::{Marshal, MarshalSize, Unmarshal};

use beep_detector::{BeepDetector, BeepDetectorResult};

use crate::audio::AudioFrame;
use crate::config::Codec;
use crate::dtmf;
use crate::events::EndpointEvent;

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
    pub fn new(socket: Arc<UdpSocket>, remote_addr: SocketAddr, codec: Codec, cancel: CancellationToken) -> Self {
        Self { socket, remote_addr, ssrc: rand::random(), codec, seq: AtomicU16::new(0), timestamp: AtomicU32::new(0), cancel }
    }

    async fn send_packet(&self, pt: u8, ts: u32, marker: bool, payload: Vec<u8>) -> std::io::Result<()> {
        let seq = self.seq.fetch_add(1, Ordering::Relaxed);
        let pkt = Packet {
            header: Header { version: 2, marker, payload_type: pt, sequence_number: seq, timestamp: ts, ssrc: self.ssrc, ..Default::default() },
            payload: bytes::Bytes::from(payload),
        };
        let buf = pkt.marshal().map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e.to_string()))?;
        self.socket.send_to(&buf, self.remote_addr).await?;
        Ok(())
    }

    pub async fn send_dtmf_event(&self, digit: char, duration_samples: u16) -> std::io::Result<()> {
        let event_code = dtmf::digit_to_event(digit).unwrap_or(0);
        let ts = self.timestamp.load(Ordering::Relaxed);
        self.send_packet(DTMF_PAYLOAD_TYPE, ts, true, dtmf::encode_rfc4733(event_code, false, 10, 0).to_vec()).await?;
        for _ in 0..3 {
            self.send_packet(DTMF_PAYLOAD_TYPE, ts, false, dtmf::encode_rfc4733(event_code, true, 10, duration_samples).to_vec()).await?;
        }
        Ok(())
    }

    pub fn start_send_loop(
        self: &Arc<Self>, outgoing_rx: Receiver<Vec<i16>>,
        muted: Arc<AtomicBool>, paused: Arc<AtomicBool>,
        flush_flag: Arc<AtomicBool>, playout_notify: Arc<(Mutex<bool>, Condvar)>,
    ) -> tokio::task::JoinHandle<()> {
        let t = Arc::clone(self);
        let cancel = self.cancel.clone();
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_millis(20));
            let samples_per_frame = 160u32; // 20ms at 8kHz
            loop {
                tokio::select! { _ = cancel.cancelled() => break, _ = interval.tick() => {} }
                if flush_flag.swap(false, Ordering::Relaxed) {
                    while outgoing_rx.try_recv().is_ok() {}
                    notify_playout(&playout_notify);
                    continue;
                }
                let ts = t.timestamp.fetch_add(samples_per_frame, Ordering::Relaxed);
                if paused.load(Ordering::Relaxed) {
                    let silence = vec![silence_byte(t.codec); samples_per_frame as usize];
                    let _ = t.send_packet(t.codec.payload_type(), ts, false, silence).await;
                    continue;
                }
                match outgoing_rx.try_recv() {
                    Ok(samples_16k) => {
                        let payload = if muted.load(Ordering::Relaxed) {
                            vec![silence_byte(t.codec); samples_per_frame as usize]
                        } else {
                            g711_encode(&resample_16k_to_8k(&samples_16k), t.codec)
                        };
                        let _ = t.send_packet(t.codec.payload_type(), ts, false, payload).await;
                    }
                    Err(_) => {
                        let _ = t.send_packet(t.codec.payload_type(), ts, false, vec![silence_byte(t.codec); samples_per_frame as usize]).await;
                        notify_playout(&playout_notify);
                    }
                }
            }
        })
    }

    pub fn start_recv_loop(
        self: &Arc<Self>, incoming_tx: Sender<AudioFrame>, event_tx: Sender<EndpointEvent>,
        call_id: i32, beep_detector: Arc<Mutex<Option<BeepDetector>>>,
    ) -> tokio::task::JoinHandle<()> {
        let t = Arc::clone(self);
        let cancel = self.cancel.clone();
        tokio::spawn(async move {
            let mut buf = vec![0u8; 2048];
            loop {
                tokio::select! {
                    _ = cancel.cancelled() => break,
                    result = t.socket.recv_from(&mut buf) => {
                        let (len, _) = match result { Ok(r) => r, Err(_) => continue };
                        let pkt = match Packet::unmarshal(&mut &buf[..len]) {
                            Ok(p) => p, Err(_) => continue,
                        };
                        if pkt.header.payload_type == DTMF_PAYLOAD_TYPE {
                            if let Some((event, is_end, _, _)) = dtmf::decode_rfc4733(&pkt.payload) {
                                if is_end { if let Some(d) = dtmf::event_to_digit(event) {
                                    let _ = event_tx.try_send(EndpointEvent::DtmfReceived { call_id, digit: d, method: "rfc2833".into() });
                                }}
                            }
                            continue;
                        }
                        let samples_8k = g711_decode(&pkt.payload, t.codec);
                        let samples_16k = resample_8k_to_16k(&samples_8k);
                        // Feed beep detector
                        if let Ok(mut guard) = beep_detector.lock() {
                            if let Some(ref mut det) = *guard {
                                match det.process_frame(&samples_16k) {
                                    BeepDetectorResult::Detected(e) => {
                                        let _ = event_tx.try_send(EndpointEvent::BeepDetected { call_id, frequency_hz: e.frequency_hz, duration_ms: e.duration_ms });
                                        *guard = None;
                                    }
                                    BeepDetectorResult::Timeout => { let _ = event_tx.try_send(EndpointEvent::BeepTimeout { call_id }); *guard = None; }
                                    BeepDetectorResult::Listening => {}
                                }
                            }
                        }
                        let n = samples_16k.len() as u32;
                        let _ = incoming_tx.try_send(AudioFrame { data: samples_16k, sample_rate: 16000, num_channels: 1, samples_per_channel: n });
                    }
                }
            }
        })
    }
}

fn notify_playout(p: &Arc<(Mutex<bool>, Condvar)>) {
    if let Ok(mut done) = p.0.lock() { *done = true; p.1.notify_all(); }
}

// ─── G.711 via audio-codec-algorithms ────────────────────────────────────────

fn g711_encode(samples: &[i16], codec: Codec) -> Vec<u8> {
    match codec {
        Codec::PCMU => samples.iter().map(|&s| audio_codec_algorithms::encode_ulaw(s)).collect(),
        Codec::PCMA => samples.iter().map(|&s| audio_codec_algorithms::encode_alaw(s)).collect(),
        _ => samples.iter().map(|&s| audio_codec_algorithms::encode_ulaw(s)).collect(),
    }
}

fn g711_decode(bytes: &[u8], codec: Codec) -> Vec<i16> {
    match codec {
        Codec::PCMU => bytes.iter().map(|&b| audio_codec_algorithms::decode_ulaw(b)).collect(),
        Codec::PCMA => bytes.iter().map(|&b| audio_codec_algorithms::decode_alaw(b)).collect(),
        _ => bytes.iter().map(|&b| audio_codec_algorithms::decode_ulaw(b)).collect(),
    }
}

fn silence_byte(codec: Codec) -> u8 {
    match codec { Codec::PCMU => 0xFF, Codec::PCMA => 0xD5, _ => 0xFF }
}

// ─── Resampling (linear interpolation, from rtpsip pattern) ──────────────────

fn resample_8k_to_16k(input: &[i16]) -> Vec<i16> {
    let mut out = Vec::with_capacity(input.len() * 2);
    for i in 0..input.len() {
        out.push(input[i]);
        let next = if i + 1 < input.len() { input[i + 1] } else { input[i] };
        out.push(((input[i] as i32 + next as i32) / 2) as i16);
    }
    out
}

fn resample_16k_to_8k(input: &[i16]) -> Vec<i16> {
    input.iter().step_by(2).copied().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_pcmu_roundtrip() {
        for &s in &[0i16, 100, 1000, 8000, -100, -1000, -8000] {
            let enc = audio_codec_algorithms::encode_ulaw(s);
            let dec = audio_codec_algorithms::decode_ulaw(enc);
            let diff = (s as i32 - dec as i32).unsigned_abs();
            assert!(diff < (s.unsigned_abs() as u32 / 10).max(100), "PCMU: {} -> {} (diff {})", s, dec, diff);
        }
    }

    #[test]
    fn test_pcma_roundtrip() {
        for &s in &[0i16, 100, 1000, 8000, -100, -1000, -8000] {
            let enc = audio_codec_algorithms::encode_alaw(s);
            let dec = audio_codec_algorithms::decode_alaw(enc);
            let diff = (s as i32 - dec as i32).unsigned_abs();
            assert!(diff < (s.unsigned_abs() as u32 / 10).max(100), "PCMA: {} -> {} (diff {})", s, dec, diff);
        }
    }

    #[test]
    fn test_resample_8k_16k_length() {
        let input = vec![0i16; 160]; // 20ms at 8kHz
        assert_eq!(resample_8k_to_16k(&input).len(), 320); // 20ms at 16kHz
    }

    #[test]
    fn test_resample_16k_8k_length() {
        let input = vec![0i16; 320]; // 20ms at 16kHz
        assert_eq!(resample_16k_to_8k(&input).len(), 160); // 20ms at 8kHz
    }

    #[test]
    fn test_g711_encode_decode() {
        let samples = vec![0i16, 1000, -1000, 8000, -8000];
        let encoded = g711_encode(&samples, Codec::PCMU);
        assert_eq!(encoded.len(), 5);
        let decoded = g711_decode(&encoded, Codec::PCMU);
        assert_eq!(decoded.len(), 5);
    }
}
