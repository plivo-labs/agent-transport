//! RTP transport — audio send/recv over UDP with G.711 codec.
//!
//! Handles: symmetric RTP, SSRC tracking, media timeout, marker bit,
//! packet validation, DTMF, NAT keepalive.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU16, AtomicU32, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use crossbeam_channel::{Receiver, Sender};
use rtp::{header::Header, packet::Packet};
use tokio::net::UdpSocket;
use tokio_util::sync::CancellationToken;
use tracing::{info, warn};
use webrtc_util::marshal::{Marshal, MarshalSize, Unmarshal};

use beep_detector::{BeepDetector, BeepDetectorResult};
use crate::audio::AudioFrame;
use crate::config::Codec;
use crate::dtmf;
use crate::events::EndpointEvent;

pub(crate) const DEFAULT_DTMF_PT: u8 = 101;
const MEDIA_TIMEOUT: Duration = Duration::from_secs(30);
const NAT_KEEPALIVE: Duration = Duration::from_secs(15);
const DTMF_END_TIMEOUT: Duration = Duration::from_secs(5);

pub(crate) struct RtpTransport {
    pub socket: Arc<UdpSocket>,
    pub remote_addr: Mutex<SocketAddr>,
    ssrc: u32, codec: Codec, seq: AtomicU16, timestamp: AtomicU32,
    pub dtmf_pt: u8, pub ptime_ms: u32, pub cancel: CancellationToken,
}

impl RtpTransport {
    pub fn new(socket: Arc<UdpSocket>, remote: SocketAddr, codec: Codec, cancel: CancellationToken, dtmf_pt: u8, ptime_ms: u32) -> Self {
        Self { socket, remote_addr: Mutex::new(remote), ssrc: rand::random(), codec, seq: AtomicU16::new(0), timestamp: AtomicU32::new(0), dtmf_pt, ptime_ms, cancel }
    }

    fn remote(&self) -> SocketAddr { *self.remote_addr.lock().unwrap() }
    fn spf(&self) -> u32 { 8000 * self.ptime_ms / 1000 }

    async fn send(&self, pt: u8, ts: u32, marker: bool, payload: Vec<u8>) -> std::io::Result<()> {
        let pkt = Packet { header: Header { version: 2, marker, payload_type: pt, sequence_number: self.seq.fetch_add(1, Ordering::Relaxed), timestamp: ts, ssrc: self.ssrc, ..Default::default() }, payload: bytes::Bytes::from(payload) };
        self.socket.send_to(&pkt.marshal().map_err(|e| std::io::Error::other(e.to_string()))?, self.remote()).await?;
        Ok(())
    }

    pub async fn send_dtmf_event(&self, digit: char, duration_ms: u32) -> std::io::Result<()> {
        let ev = dtmf::digit_to_event(digit).unwrap_or(0);
        let ts = self.timestamp.load(Ordering::Relaxed);
        let dur = (8 * duration_ms) as u16;
        let pt = self.ptime_ms as u64;
        self.send(self.dtmf_pt, ts, true, dtmf::encode_rfc4733(ev, false, 10, 0).to_vec()).await?;
        tokio::time::sleep(Duration::from_millis(pt)).await;
        let steps = (duration_ms / self.ptime_ms).max(1);
        for i in 1..=steps {
            self.send(self.dtmf_pt, ts, false, dtmf::encode_rfc4733(ev, false, 10, ((8 * self.ptime_ms * i) as u16).min(dur)).to_vec()).await?;
            tokio::time::sleep(Duration::from_millis(pt)).await;
        }
        for _ in 0..3 {
            self.send(self.dtmf_pt, ts, false, dtmf::encode_rfc4733(ev, true, 10, dur).to_vec()).await?;
            tokio::time::sleep(Duration::from_millis(pt)).await;
        }
        Ok(())
    }

    pub fn start_send_loop(self: &Arc<Self>, rx: Receiver<Vec<i16>>, muted: Arc<AtomicBool>, paused: Arc<AtomicBool>, flush: Arc<AtomicBool>, playout: Arc<(Mutex<bool>, Condvar)>) -> tokio::task::JoinHandle<()> {
        let t = Arc::clone(self);
        tokio::spawn(async move {
            let mut iv = tokio::time::interval(Duration::from_millis(t.ptime_ms as u64));
            let (sil, spf) = (t.codec.silence_byte(), t.spf());
            let mut first = true;
            loop {
                tokio::select! { _ = t.cancel.cancelled() => break, _ = iv.tick() => {} }
                let ts = t.timestamp.fetch_add(spf, Ordering::Relaxed);
                if flush.swap(false, Ordering::Relaxed) { while rx.try_recv().is_ok() {} notify(&playout); first = true; continue; }
                if paused.load(Ordering::Relaxed) { let _ = t.send(t.codec.payload_type(), ts, false, vec![sil; spf as usize]).await; continue; }
                match rx.try_recv() {
                    Ok(s) if !muted.load(Ordering::Relaxed) => { let m = first; first = false; let _ = t.send(t.codec.payload_type(), ts, m, t.codec.encode(&s.iter().step_by(2).copied().collect::<Vec<_>>())).await; }
                    Ok(_) => { let _ = t.send(t.codec.payload_type(), ts, false, vec![sil; spf as usize]).await; }
                    Err(_) => { notify(&playout); first = true; let _ = t.send(t.codec.payload_type(), ts, false, vec![sil; spf as usize]).await; }
                }
            }
        })
    }

    pub fn start_recv_loop(self: &Arc<Self>, tx: Sender<AudioFrame>, etx: Sender<EndpointEvent>, cid: i32, bd: Arc<Mutex<Option<BeepDetector>>>) -> tokio::task::JoinHandle<()> {
        let t = Arc::clone(self);
        tokio::spawn(async move {
            let mut buf = vec![0u8; 2048];
            let (mut last_rtp, mut remote_ssrc) = (Instant::now(), None::<u32>);
            let mut ka = tokio::time::interval(NAT_KEEPALIVE);
            let (mut dtmf_ev, mut dtmf_timer): (Option<u8>, Option<Instant>) = (None, None);

            loop {
                tokio::select! {
                    _ = t.cancel.cancelled() => break,
                    _ = ka.tick() => { let ts = t.timestamp.load(Ordering::Relaxed); let _ = t.send(t.codec.payload_type(), ts, false, vec![t.codec.silence_byte(); t.spf() as usize]).await; }
                    r = t.socket.recv_from(&mut buf) => {
                        let (len, from) = match r { Ok(r) => r, Err(_) => continue };
                        if len < 12 { continue; }
                        let pkt = match Packet::unmarshal(&mut &buf[..len]) { Ok(p) => p, Err(_) => continue };
                        if pkt.header.version != 2 { continue; }

                        // Symmetric RTP
                        if from != t.remote() { info!("Symmetric RTP: {} -> {}", t.remote(), from); *t.remote_addr.lock().unwrap() = from; }

                        // SSRC tracking
                        let ss = pkt.header.ssrc;
                        if let Some(k) = remote_ssrc { if ss != k && ss != t.ssrc { info!("SSRC change: {} -> {}", k, ss); remote_ssrc = Some(ss); dtmf_ev = None; } }
                        else if ss != t.ssrc { remote_ssrc = Some(ss); }
                        last_rtp = Instant::now();

                        // DTMF
                        if pkt.header.payload_type == t.dtmf_pt {
                            if let Some((ev, end, _, _)) = dtmf::decode_rfc4733(&pkt.payload) {
                                if end { if let Some(d) = dtmf::event_to_digit(ev) { let _ = etx.try_send(EndpointEvent::DtmfReceived { call_id: cid, digit: d, method: "rfc2833".into() }); } dtmf_ev = None; dtmf_timer = None; }
                                else { dtmf_ev = Some(ev); if dtmf_timer.is_none() { dtmf_timer = Some(Instant::now()); } }
                            }
                            continue;
                        }
                        // DTMF END timeout
                        if let Some(ev) = dtmf_ev { if dtmf_timer.map(|t| t.elapsed() > DTMF_END_TIMEOUT).unwrap_or(false) { if let Some(d) = dtmf::event_to_digit(ev) { warn!("DTMF END timeout: {}", d); let _ = etx.try_send(EndpointEvent::DtmfReceived { call_id: cid, digit: d, method: "rfc2833".into() }); } dtmf_ev = None; dtmf_timer = None; } }
                        if pkt.header.payload_type != t.codec.payload_type() { continue; }

                        // Decode + upsample (8k->16k inline)
                        let s8 = t.codec.decode(&pkt.payload);
                        let mut pcm = Vec::with_capacity(s8.len() * 2);
                        for i in 0..s8.len() { pcm.push(s8[i]); let n = if i + 1 < s8.len() { s8[i + 1] } else { s8[i] }; pcm.push(((s8[i] as i32 + n as i32) / 2) as i16); }

                        // Beep detector
                        if let Ok(mut g) = bd.lock() { if let Some(ref mut det) = *g {
                            match det.process_frame(&pcm) {
                                BeepDetectorResult::Detected(e) => { let _ = etx.try_send(EndpointEvent::BeepDetected { call_id: cid, frequency_hz: e.frequency_hz, duration_ms: e.duration_ms }); *g = None; }
                                BeepDetectorResult::Timeout => { let _ = etx.try_send(EndpointEvent::BeepTimeout { call_id: cid }); *g = None; }
                                _ => {}
                            }
                        }}
                        let n = pcm.len() as u32;
                        let _ = tx.try_send(AudioFrame { data: pcm, sample_rate: 16000, num_channels: 1, samples_per_channel: n });
                    }
                }
                if last_rtp.elapsed() > MEDIA_TIMEOUT {
                    warn!("Media timeout call {} ({}s)", cid, MEDIA_TIMEOUT.as_secs());
                    let _ = etx.try_send(EndpointEvent::CallTerminated { session: crate::call::CallSession::new(cid, crate::call::CallDirection::Outbound), reason: "media timeout".into() });
                    break;
                }
            }
        })
    }
}

fn notify(p: &Arc<(Mutex<bool>, Condvar)>) { if let Ok(mut d) = p.0.lock() { *d = true; p.1.notify_all(); } }

#[cfg(test)]
mod tests {
    use super::*;
    use audio_codec_algorithms::{encode_ulaw, decode_ulaw, encode_alaw, decode_alaw};

    #[test]
    fn test_pcmu_roundtrip() {
        for &s in &[0i16, 100, 1000, 8000, -100, -1000, -8000] {
            let d = decode_ulaw(encode_ulaw(s));
            assert!((s as i32 - d as i32).unsigned_abs() < (s.unsigned_abs() as u32 / 10).max(100), "PCMU: {s} -> {d}");
        }
    }

    #[test]
    fn test_pcma_roundtrip() {
        for &s in &[0i16, 100, 1000, 8000, -100, -1000, -8000] {
            let d = decode_alaw(encode_alaw(s));
            assert!((s as i32 - d as i32).unsigned_abs() < (s.unsigned_abs() as u32 / 10).max(100), "PCMA: {s} -> {d}");
        }
    }

    #[test]
    fn test_codec_encode_decode() {
        let s = vec![0i16, 1000, -1000, 8000];
        assert_eq!(Codec::PCMU.encode(&s).len(), 4);
        assert_eq!(Codec::PCMU.decode(&Codec::PCMU.encode(&s)).len(), 4);
    }

    #[test]
    fn test_codec_silence() {
        assert_eq!(Codec::PCMU.silence_byte(), 0xFF);
        assert_eq!(Codec::PCMA.silence_byte(), 0xD5);
    }
}
