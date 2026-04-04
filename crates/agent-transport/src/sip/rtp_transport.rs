//! RTP transport — audio send/recv over UDP with G.711 codec.
//!
//! Handles: symmetric RTP, SSRC tracking, media timeout, marker bit,
//! packet validation, DTMF, NAT keepalive.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU16, AtomicU32, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use crossbeam_channel::Sender;
use rtp::{header::Header, packet::Packet};
use tokio::net::UdpSocket;
use tokio_util::sync::CancellationToken;
use tracing::{debug, info, warn};
use webrtc_util::marshal::{Marshal, Unmarshal};

use beep_detector::{BeepDetector, BeepDetectorResult};
use crate::audio::AudioFrame;
use crate::config::Codec;
use crate::recorder::CallRecorder;
use crate::sip::audio_buffer::AudioBuffer;
use crate::sip::dtmf;
use crate::sip::resampler::Resampler;
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
    /// Input sample rate — codec audio is resampled to this rate for delivery.
    pub input_sample_rate: u32,
    /// Output sample rate — TTS audio at this rate is resampled to codec rate.
    pub output_sample_rate: u32,
}

impl RtpTransport {
    pub fn new(socket: Arc<UdpSocket>, remote: SocketAddr, codec: Codec, cancel: CancellationToken, dtmf_pt: u8, ptime_ms: u32, input_sample_rate: u32, output_sample_rate: u32) -> Self {
        Self { socket, remote_addr: Mutex::new(remote), ssrc: rand::random(), codec, seq: AtomicU16::new(0), timestamp: AtomicU32::new(0), dtmf_pt, ptime_ms, cancel, input_sample_rate, output_sample_rate }
    }

    fn remote(&self) -> SocketAddr { *self.remote_addr.lock().unwrap_or_else(|e| e.into_inner()) }
    fn spf(&self) -> u32 { self.codec.sample_rate() * self.ptime_ms / 1000 }

    async fn send(&self, pt: u8, ts: u32, marker: bool, payload: Vec<u8>) -> std::io::Result<()> {
        let pkt = Packet { header: Header { version: 2, marker, payload_type: pt, sequence_number: self.seq.fetch_add(1, Ordering::Relaxed), timestamp: ts, ssrc: self.ssrc, ..Default::default() }, payload: bytes::Bytes::from(payload) };
        self.socket.send_to(&pkt.marshal().map_err(|e| std::io::Error::other(e.to_string()))?, self.remote()).await?;
        Ok(())
    }

    pub async fn send_dtmf_event(&self, digit: char, duration_ms: u32) -> std::io::Result<()> {
        let ev = dtmf::digit_to_event(digit).unwrap_or(0);
        let ts = self.timestamp.load(Ordering::Relaxed);
        let dur = (8u32.saturating_mul(duration_ms)).min(u16::MAX as u32) as u16;
        let pt = self.ptime_ms as u64;
        self.send(self.dtmf_pt, ts, true, dtmf::encode_rfc4733(ev, false, 10, 0).to_vec()).await?;
        tokio::time::sleep(Duration::from_millis(pt)).await;
        let steps = (duration_ms / self.ptime_ms).max(1);
        for i in 1..=steps {
            let step_dur = (8u32.saturating_mul(self.ptime_ms).saturating_mul(i)).min(dur as u32) as u16;
            self.send(self.dtmf_pt, ts, false, dtmf::encode_rfc4733(ev, false, 10, step_dur).to_vec()).await?;
            tokio::time::sleep(Duration::from_millis(pt)).await;
        }
        for _ in 0..3 {
            self.send(self.dtmf_pt, ts, false, dtmf::encode_rfc4733(ev, true, 10, dur).to_vec()).await?;
            tokio::time::sleep(Duration::from_millis(pt)).await;
        }
        Ok(())
    }

    /// Start the RTP send loop. Drains from the shared AudioBuffer every ptime_ms.
    /// Matches WebRTC C++ InternalSource::audio_task_ (10ms repeating task).
    pub fn start_send_loop(self: &Arc<Self>, audio_buf: Arc<AudioBuffer>, bg_audio_buf: Arc<AudioBuffer>, muted: Arc<AtomicBool>, paused: Arc<AtomicBool>, playout: Arc<(Mutex<bool>, Condvar)>, recorder: Arc<Mutex<Option<Arc<CallRecorder>>>>) -> tokio::task::JoinHandle<()> {
        let t = Arc::clone(self);
        tokio::spawn(async move {
            let mut iv = tokio::time::interval(Duration::from_millis(t.ptime_ms as u64));
            let (sil, spf) = (t.codec.silence_byte(), t.spf());
            let output_spf = (t.output_sample_rate * t.ptime_ms / 1000) as usize;
            let mut first = true;
            let mut pkt_count = 0u32;
            let mut octet_count = 0u32;
            let mut rtcp_iv = tokio::time::interval(Duration::from_secs(5));
            rtcp_iv.tick().await;
            let output_rate = t.output_sample_rate;
            let mut downsampler = Resampler::new_voip(output_rate, t.codec.sample_rate());

            loop {
                tokio::select! {
                    _ = t.cancel.cancelled() => break,
                    _ = rtcp_iv.tick() => {
                        let ts = t.timestamp.load(Ordering::Relaxed);
                        let sr = super::rtcp::build_sender_report(t.ssrc, ts, pkt_count, octet_count);
                        let _ = t.socket.send_to(&sr, t.remote()).await;
                        let buf_ms = (audio_buf.len() as u32 * 1000) / output_rate;
                        debug!("RTP TX: pkts={} octets={} buf={}ms codec={:?} remote={}", pkt_count, octet_count, buf_ms, t.codec, t.remote());
                    }
                    _ = iv.tick() => {}
                }

                let ts = t.timestamp.fetch_add(spf, Ordering::Relaxed);

                // Drain background audio regardless of pause state
                let bg_samples = bg_audio_buf.drain(output_spf);

                if paused.load(Ordering::Relaxed) {
                    // Paused: send background audio only (no agent voice)
                    if !bg_samples.is_empty() {
                        let samples_8k = if let Some(ref mut ds) = downsampler {
                            ds.process(&bg_samples).to_vec()
                        } else {
                            bg_samples
                        };
                        let encoded = t.codec.encode(&samples_8k);
                        octet_count += encoded.len() as u32;
                        let _ = t.send(t.codec.payload_type(), ts, false, encoded).await;
                    } else {
                        let _ = t.send(t.codec.payload_type(), ts, false, vec![sil; spf as usize]).await;
                    }
                    pkt_count += 1;
                    continue;
                }

                // Drain agent voice and mix with background
                let voice = audio_buf.drain(output_spf);

                let has_voice = !voice.is_empty();
                let has_bg = !bg_samples.is_empty();
                let samples = if has_voice && has_bg {
                    let len = voice.len().max(bg_samples.len());
                    let mut out = Vec::with_capacity(len);
                    for i in 0..len {
                        let v = if i < voice.len() { voice[i] as i32 } else { 0 };
                        let b = if i < bg_samples.len() { bg_samples[i] as i32 } else { 0 };
                        out.push((v + b).clamp(-32768, 32767) as i16);
                    }
                    out
                } else if has_voice {
                    voice
                } else {
                    bg_samples
                };

                // Record agent audio — always write to keep in sync with user channel
                if let Ok(guard) = recorder.lock() {
                    if let Some(ref rec) = *guard {
                        if !samples.is_empty() {
                            rec.write_agent_samples(&samples);
                        } else {
                            // Write silence to keep agent channel aligned with user channel
                            rec.write_agent_samples(&vec![0i16; output_spf]);
                        }
                    }
                }

                if !samples.is_empty() {
                    if !muted.load(Ordering::Relaxed) {
                        let m = first; first = false;
                        let samples_8k = if let Some(ref mut ds) = downsampler {
                            ds.process(&samples).to_vec()
                        } else {
                            samples
                        };
                        let encoded = t.codec.encode(&samples_8k);
                        octet_count += encoded.len() as u32;
                        let _ = t.send(t.codec.payload_type(), ts, m, encoded).await;
                        pkt_count += 1;
                    } else {
                        let _ = t.send(t.codec.payload_type(), ts, false, vec![sil; spf as usize]).await;
                        pkt_count += 1; octet_count += spf;
                    }
                } else {
                    // No audio — send silence, notify playout completion
                    if audio_buf.is_empty() {
                        notify(&playout);
                        first = true;
                    }
                    let _ = t.send(t.codec.payload_type(), ts, false, vec![sil; spf as usize]).await;
                    pkt_count += 1; octet_count += spf;
                }
            }
        })
    }

    pub fn start_recv_loop(self: &Arc<Self>, tx: Sender<AudioFrame>, etx: Sender<EndpointEvent>, cid: String, direction: crate::sip::call::CallDirection, bd: Arc<Mutex<Option<BeepDetector>>>, held: Arc<AtomicBool>, recorder: Arc<Mutex<Option<Arc<CallRecorder>>>>) -> tokio::task::JoinHandle<()> {
        let t = Arc::clone(self);
        tokio::spawn(async move {
            let mut buf = vec![0u8; 2048];
            let (mut last_rtp, mut remote_ssrc) = (Instant::now(), None::<u32>);
            let mut ka = tokio::time::interval(NAT_KEEPALIVE);
            let (mut dtmf_ev, mut dtmf_timer): (Option<u8>, Option<Instant>) = (None, None);
            let (mut rx_pkts, mut rx_log_time) = (0u32, Instant::now());
            let input_rate = t.input_sample_rate;
            // speexdsp resampler: codec rate → input rate (same approach as FreeSWITCH)
            let mut upsampler = Resampler::new_voip(t.codec.sample_rate(), input_rate);

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

                        // DTMF (RFC 4733) — dedup END retransmissions
                        if pkt.header.payload_type == t.dtmf_pt {
                            if let Some((ev, end, _vol, _dur)) = dtmf::decode_rfc4733(&pkt.payload) {
                                if end {
                                    // Only emit once per digit — dtmf_ev is Some during active event
                                    if dtmf_ev.is_some() {
                                        if let Some(d) = dtmf::event_to_digit(ev) {
                                            debug!("DTMF digit: {}", d);
                                            let _ = etx.try_send(EndpointEvent::DtmfReceived { call_id: cid.clone(), digit: d, method: "rfc2833".into() });
                                        }
                                        dtmf_ev = None;
                                        dtmf_timer = None;
                                    }
                                    // else: END retransmission — already handled, ignore
                                } else {
                                    dtmf_ev = Some(ev);
                                    if dtmf_timer.is_none() { dtmf_timer = Some(Instant::now()); }
                                }
                            }
                            continue;
                        }
                        // Log unexpected payload types
                        if pkt.header.payload_type != t.codec.payload_type() && pkt.header.payload_type != 13 {
                            debug!("RTP unexpected PT={} (expected {} or {})", pkt.header.payload_type, t.codec.payload_type(), t.dtmf_pt);
                        }
                        // DTMF END timeout
                        if let Some(ev) = dtmf_ev { if dtmf_timer.map(|t| t.elapsed() > DTMF_END_TIMEOUT).unwrap_or(false) { if let Some(d) = dtmf::event_to_digit(ev) { warn!("DTMF END timeout: {}", d); let _ = etx.try_send(EndpointEvent::DtmfReceived { call_id: cid.clone(), digit: d, method: "rfc2833".into() }); } dtmf_ev = None; dtmf_timer = None; } }
                        if pkt.header.payload_type != t.codec.payload_type() { continue; }

                        // Decode G.711, then resample codec rate → pipeline rate via speexdsp
                        let s8 = t.codec.decode(&pkt.payload);
                        let pcm = if let Some(ref mut us) = upsampler {
                            us.process(&s8).to_vec()
                        } else {
                            s8 // same rate — no resampling
                        };

                        // Record user audio (pipeline rate, after resample)
                        if let Ok(guard) = recorder.lock() { if let Some(ref rec) = *guard { rec.write_user_samples(&pcm); } }

                        // Beep detector
                        if let Ok(mut g) = bd.lock() { if let Some(ref mut det) = *g {
                            match det.process_frame(&pcm) {
                                BeepDetectorResult::Detected(e) => { let _ = etx.try_send(EndpointEvent::BeepDetected { call_id: cid.clone(), frequency_hz: e.frequency_hz, duration_ms: e.duration_ms }); *g = None; }
                                BeepDetectorResult::Timeout => { let _ = etx.try_send(EndpointEvent::BeepTimeout { call_id: cid.clone() }); *g = None; }
                                _ => {}
                            }
                        }}
                        let n = pcm.len() as u32;
                        let _ = tx.try_send(AudioFrame { data: pcm, sample_rate: input_rate, num_channels: 1, samples_per_channel: n });
                        rx_pkts += 1;
                        if rx_log_time.elapsed() >= Duration::from_secs(5) {
                            debug!("RTP RX: pkts={} ssrc={:?} remote={}", rx_pkts, remote_ssrc, from);
                            rx_log_time = Instant::now();
                        }
                    }
                }
                // Skip media timeout check during SIP hold (remote is expected to stop sending)
                if last_rtp.elapsed() > MEDIA_TIMEOUT && !held.load(Ordering::Relaxed) {
                    warn!("Media timeout call {} ({}s)", cid, MEDIA_TIMEOUT.as_secs());
                    let _ = etx.try_send(EndpointEvent::CallTerminated { session: crate::sip::call::CallSession::new(cid, direction), reason: "media timeout".into() });
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
