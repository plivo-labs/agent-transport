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
use crate::sync::LockExt;

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

    fn remote(&self) -> SocketAddr { *self.remote_addr.lock_or_recover() }
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

            // ── BG audio instrumentation (debug only) ───────────────────
            // Counts per 5s RTCP window so we can observe whether the
            // Python forwarder is starving bg_audio_buf (empty on drain)
            // or keeping it healthy.
            let mut bg_empty_ticks: u32 = 0;
            let mut bg_partial_ticks: u32 = 0; // 1..output_spf-1 samples
            let mut bg_full_ticks: u32 = 0;    // == output_spf samples
            let mut bg_sample_sum: u64 = 0;    // total samples drained this window
            let mut bg_buf_len_sum: u64 = 0;   // sum of bg_audio_buf.len() BEFORE drain
            let mut bg_buf_len_max: usize = 0;
            // Sample-energy counters so we can tell whether the frames
            // contain real audio or zero-valued silence (underrun from
            // LiveKit's native AudioSource).
            let mut bg_silent_ticks: u32 = 0; // ticks where all samples were 0
            let mut bg_loud_ticks: u32 = 0;   // ticks with any |sample| > 100
            let mut bg_abs_sum: u64 = 0;      // sum of |sample| across all drained samples

            loop {
                tokio::select! {
                    _ = t.cancel.cancelled() => break,
                    _ = rtcp_iv.tick() => {
                        let ts = t.timestamp.load(Ordering::Relaxed);
                        let sr = super::rtcp::build_sender_report(t.ssrc, ts, pkt_count, octet_count);
                        let _ = t.socket.send_to(&sr, t.remote()).await;
                        let buf_ms = (audio_buf.len() as u32 * 1000) / output_rate;
                        debug!("RTP TX: pkts={} octets={} buf={}ms codec={:?} remote={}", pkt_count, octet_count, buf_ms, t.codec, t.remote());

                        // BG audio health report
                        let total_ticks = bg_empty_ticks + bg_partial_ticks + bg_full_ticks;
                        if total_ticks > 0 {
                            let avg_before_drain = bg_buf_len_sum / total_ticks as u64;
                            let avg_abs = if bg_sample_sum > 0 {
                                bg_abs_sum / bg_sample_sum
                            } else {
                                0
                            };
                            debug!(
                                "BG TX health (5s): ticks={} empty={} partial={} full={} drained={} avg_buf={}ms max_buf={}samples | silent_ticks={} loud_ticks={} avg_abs_sample={}",
                                total_ticks,
                                bg_empty_ticks,
                                bg_partial_ticks,
                                bg_full_ticks,
                                bg_sample_sum,
                                avg_before_drain * 1000 / output_rate as u64,
                                bg_buf_len_max,
                                bg_silent_ticks,
                                bg_loud_ticks,
                                avg_abs,
                            );
                        }
                        bg_empty_ticks = 0;
                        bg_partial_ticks = 0;
                        bg_full_ticks = 0;
                        bg_sample_sum = 0;
                        bg_buf_len_sum = 0;
                        bg_buf_len_max = 0;
                        bg_silent_ticks = 0;
                        bg_loud_ticks = 0;
                        bg_abs_sum = 0;
                    }
                    _ = iv.tick() => {}
                }

                let ts = t.timestamp.fetch_add(spf, Ordering::Relaxed);

                // Snapshot bg buffer length BEFORE drain so we can see if
                // Python is filling it or if we're racing empty.
                let bg_buf_len_before = bg_audio_buf.len();
                bg_buf_len_sum += bg_buf_len_before as u64;
                if bg_buf_len_before > bg_buf_len_max {
                    bg_buf_len_max = bg_buf_len_before;
                }

                // Drain background audio regardless of pause state
                let bg_samples = bg_audio_buf.drain(output_spf);

                // Categorize this tick (occupancy)
                let n = bg_samples.len();
                bg_sample_sum += n as u64;
                if n == 0 {
                    bg_empty_ticks += 1;
                } else if n < output_spf {
                    bg_partial_ticks += 1;
                } else {
                    bg_full_ticks += 1;
                }

                // Categorize this tick (energy) — detects silent frames
                // coming from LiveKit's native AudioSource underrun.
                if n > 0 {
                    let mut max_abs: i32 = 0;
                    let mut sum_abs: u64 = 0;
                    for &s in &bg_samples {
                        let a = s.unsigned_abs() as i32;
                        if a > max_abs {
                            max_abs = a;
                        }
                        sum_abs += a as u64;
                    }
                    bg_abs_sum += sum_abs;
                    if max_abs == 0 {
                        bg_silent_ticks += 1;
                    } else if max_abs > 100 {
                        bg_loud_ticks += 1;
                    }
                }

                if paused.load(Ordering::Acquire) {
                    // Paused: send background audio only (no agent voice).
                    // Record the SAME samples we're putting on the wire so the
                    // agent channel stays time-aligned with the user channel
                    // (which the recv loop keeps writing unconditionally).
                    // Without this write the recorder's user/agent VecDeques
                    // drift and interleave() pads trailing zeros that show up
                    // as silent gaps straddling every pause/resume cycle.
                    {
                        let guard = recorder.lock_or_recover();
                        if let Some(ref rec) = *guard {
                            if !bg_samples.is_empty() {
                                rec.write_agent_samples(&bg_samples);
                            } else {
                                rec.write_agent_samples(&vec![0i16; output_spf]);
                            }
                        }
                    }
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
                {
                    let guard = recorder.lock_or_recover();
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
                    if !muted.load(Ordering::Acquire) {
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
                    // No audio — send silence
                    let _ = t.send(t.codec.payload_type(), ts, false, vec![sil; spf as usize]).await;
                    pkt_count += 1; octet_count += spf;
                }

                // After draining voice: notify playout completion when voice buffer is empty.
                // This is independent of background audio — bg doesn't block voice playout tracking.
                // Without this, continuously flowing bg audio keeps `samples` non-empty and the
                // playout condvar never fires, hanging wait_for_playout callers.
                if audio_buf.is_empty() {
                    notify(&playout);
                    // Reset marker bit only when nothing (neither voice nor bg) is flowing,
                    // so the next talk burst gets marker=1 (matches WebRTC behavior).
                    if !has_voice && !has_bg {
                        first = true;
                    }
                }
            }
        })
    }

    pub fn start_recv_loop(self: &Arc<Self>, tx: Sender<AudioFrame>, etx: Sender<EndpointEvent>, cid: String, terminated: Arc<AtomicBool>, session: crate::sip::call::CallSession, bd: Arc<Mutex<Option<BeepDetector>>>, held: Arc<AtomicBool>, recorder: Arc<Mutex<Option<Arc<CallRecorder>>>>) -> tokio::task::JoinHandle<()> {
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

            // Symmetric-RTP hysteresis: only migrate the remote address after we
            // see SYMMETRIC_RTP_CONFIRM consecutive packets from the same new
            // source. Without this, a NAT that re-binds every few packets causes
            // remote_addr to flap on every receive, producing audio instability.
            const SYMMETRIC_RTP_CONFIRM: u32 = 3;
            let mut pending_remote: Option<(SocketAddr, u32)> = None;

            loop {
                tokio::select! {
                    _ = t.cancel.cancelled() => break,
                    _ = ka.tick() => { let ts = t.timestamp.load(Ordering::Relaxed); let _ = t.send(t.codec.payload_type(), ts, false, vec![t.codec.silence_byte(); t.spf() as usize]).await; }
                    r = t.socket.recv_from(&mut buf) => {
                        let (len, from) = match r { Ok(r) => r, Err(_) => continue };
                        if len < 12 { continue; }

                        // RFC 5761 RTP/RTCP de-mux: when the peer negotiated
                        // a=rtcp-mux, RTCP packets arrive on the same UDP
                        // port as RTP. Detect and skip them before attempting
                        // RTP parsing — otherwise rtp::Packet::unmarshal
                        // misinterprets the RTCP SSRC as the media SSRC and
                        // pollutes the SSRC tracker, producing flapping
                        // "SSRC change" logs every ~4 seconds (one per
                        // incoming Sender Report).
                        if super::rtcp::is_rtcp(&buf[..len]) {
                            // Still counts as the peer being alive — refresh
                            // the media timeout, but don't touch SSRC or PT.
                            // A future change can parse SR for RTT/jitter.
                            last_rtp = Instant::now();
                            continue;
                        }

                        let pkt = match Packet::unmarshal(&mut &buf[..len]) { Ok(p) => p, Err(_) => continue };
                        if pkt.header.version != 2 { continue; }

                        // Symmetric RTP with hysteresis: require N consecutive
                        // packets from a new source before migrating. Prevents
                        // remote_addr flapping under unstable NATs that re-bind
                        // the external port every few packets.
                        if from != t.remote() {
                            pending_remote = match pending_remote {
                                Some((addr, n)) if addr == from => Some((addr, n + 1)),
                                _ => Some((from, 1)),
                            };
                            if let Some((addr, n)) = pending_remote {
                                if n >= SYMMETRIC_RTP_CONFIRM {
                                    info!("Symmetric RTP: {} -> {} (after {} consecutive)", t.remote(), addr, n);
                                    *t.remote_addr.lock_or_recover() = addr;
                                    pending_remote = None;
                                }
                            }
                        } else {
                            pending_remote = None;
                        }

                        // SSRC tracking
                        let ss = pkt.header.ssrc;
                        if let Some(k) = remote_ssrc { if ss != k && ss != t.ssrc { info!("SSRC change: {} -> {}", k, ss); remote_ssrc = Some(ss); dtmf_ev = None; } }
                        else if ss != t.ssrc { remote_ssrc = Some(ss); }
                        last_rtp = Instant::now();

                        // DTMF (RFC 4733) — dedup END retransmissions
                        if pkt.header.payload_type == t.dtmf_pt {
                            if let Some((ev, end, _vol, _dur)) = dtmf::decode_rfc4733(&pkt.payload) {
                                match dtmf::dtmf_transition(dtmf_ev, ev, end) {
                                    dtmf::DtmfTransition::EndEmit { event } => {
                                        if let Some(d) = dtmf::event_to_digit(event) {
                                            debug!("DTMF digit: {}", d);
                                            let _ = etx.try_send(EndpointEvent::DtmfReceived { call_id: cid.clone(), digit: d, method: "rfc2833".into() });
                                        }
                                        dtmf_ev = None;
                                        dtmf_timer = None;
                                    }
                                    dtmf::DtmfTransition::EndStale => {}
                                    dtmf::DtmfTransition::StartOrChange { event } => {
                                        // New digit (or first packet): reset both tracker and timer.
                                        // If the previous digit's END was lost, the old timer baseline
                                        // would otherwise leak into the new digit and fire a stale timeout.
                                        dtmf_ev = Some(event);
                                        dtmf_timer = Some(Instant::now());
                                    }
                                    dtmf::DtmfTransition::StartRetransmit => {}
                                    dtmf::DtmfTransition::NoChange => {}
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
                        {
                            let guard = recorder.lock_or_recover();
                            if let Some(ref rec) = *guard { rec.write_user_samples(&pcm); }
                        }

                        // Beep detector
                        {
                            let mut g = bd.lock_or_recover();
                            if let Some(ref mut det) = *g {
                                match det.process_frame(&pcm) {
                                    BeepDetectorResult::Detected(e) => { let _ = etx.try_send(EndpointEvent::BeepDetected { call_id: cid.clone(), frequency_hz: e.frequency_hz, duration_ms: e.duration_ms }); *g = None; }
                                    BeepDetectorResult::Timeout => { let _ = etx.try_send(EndpointEvent::BeepTimeout { call_id: cid.clone() }); *g = None; }
                                    _ => {}
                                }
                            }
                        }
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
                if last_rtp.elapsed() > MEDIA_TIMEOUT && !held.load(Ordering::Acquire) {
                    warn!("Media timeout call {} ({}s)", cid, MEDIA_TIMEOUT.as_secs());
                    // Two-state termination, mirroring terminate_sip_call: flip
                    // `terminated` once (dedupe against the dialog watcher's own
                    // CallTerminated) and cancel the SHARED per-call token so the
                    // send loop stops too — previously only this recv loop broke,
                    // leaking the send-loop task. Emit the populated session
                    // (remote_uri / call_uuid / extra_headers), not a blank one.
                    if !terminated.swap(true, Ordering::AcqRel) {
                        t.cancel.cancel();
                        let _ = etx.try_send(EndpointEvent::CallTerminated { session: session.clone(), reason: "media timeout".into() });
                    }
                    break;
                }
            }
        })
    }
}

fn notify(p: &Arc<(Mutex<bool>, Condvar)>) {
    let mut d = p.0.lock_or_recover();
    *d = true;
    p.1.notify_all();
}

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
