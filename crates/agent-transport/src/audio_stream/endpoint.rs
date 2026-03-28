//! Plivo WebSocket audio streaming endpoint.
//!
//! Supports all 3 audio formats: x-l16 8kHz, x-l16 16kHz, x-mulaw 8kHz.
//! Handles: playAudio, clearAudio, checkpoint, sendDTMF commands.
//! Receives: start, media, dtmf, stop, playedStream, clearedAudio events.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use audio_codec_algorithms::{decode_ulaw, encode_ulaw};
use base64::Engine;
use crossbeam_channel::{Receiver, Sender};
use serde::Deserialize;
use tokio::net::TcpListener;
use tokio::runtime::Runtime;
use tokio_tungstenite::tungstenite::Message;
use tokio_util::sync::CancellationToken;
use tracing::{debug, error, info, warn};

use crate::audio::AudioFrame;
use crate::error::{EndpointError, Result};
use crate::events::EndpointEvent;
use crate::sip::audio_buffer::{AudioBuffer, CompletionCallback};
use super::config::AudioStreamConfig;

// ─── Plivo message types ─────────────────────────────────────────────────────

#[derive(Deserialize)]
struct PlivoMessage {
    event: String,
    #[serde(default)] start: Option<PlivoStart>,
    #[serde(default)] media: Option<PlivoMedia>,
    #[serde(default)] dtmf: Option<PlivoDtmf>,
    #[serde(rename = "streamId", default)] stream_id: Option<String>,
    #[serde(default)] name: Option<String>, // for playedStream
    // extra_headers is a TOP-LEVEL field on every Plivo event (not inside start)
    #[serde(default)] extra_headers: Option<String>,
    #[serde(rename = "sequenceNumber", default)] sequence_number: Option<u64>,
}

#[derive(Deserialize)]
struct PlivoStart {
    #[serde(rename = "callId")] call_id: String,
    #[serde(rename = "streamId")] stream_id: String,
    #[serde(default, rename = "mediaFormat")] media_format: Option<PlivoMediaFormat>,
}

#[derive(Deserialize, Clone)]
struct PlivoMediaFormat {
    #[serde(default)] encoding: String,
    #[serde(rename = "sampleRate", default)] sample_rate: u32,
}

#[derive(Deserialize)]
struct PlivoMedia {
    payload: String,
    #[serde(default)] track: Option<String>,
    #[serde(default)] timestamp: Option<String>,
    #[serde(default)] chunk: Option<u64>,
}

#[derive(Deserialize)]
struct PlivoDtmf {
    digit: String,
    #[serde(default)] timestamp: Option<String>,
}

// ─── Audio encoding detected from start event ───────────────────────────────

#[derive(Clone, Copy, Debug)]
enum AudioEncoding { MulawRate8k, L16Rate8k, L16Rate16k }

impl AudioEncoding {
    fn from_format(fmt: &PlivoMediaFormat) -> Self {
        match (fmt.encoding.as_str(), fmt.sample_rate) {
            (e, 16000) if e.contains("l16") || e.contains("L16") => AudioEncoding::L16Rate16k,
            (e, _) if e.contains("l16") || e.contains("L16") => AudioEncoding::L16Rate8k,
            _ => AudioEncoding::MulawRate8k,
        }
    }
    fn content_type(&self) -> &'static str {
        match self {
            AudioEncoding::MulawRate8k => "audio/x-mulaw",
            AudioEncoding::L16Rate8k => "audio/x-l16",
            AudioEncoding::L16Rate16k => "audio/x-l16",
        }
    }
    fn send_sample_rate(&self) -> u32 {
        match self { AudioEncoding::L16Rate16k => 16000, _ => 8000 }
    }
    fn sample_rate(&self) -> u32 { self.send_sample_rate() }
}

// ─── Session context ─────────────────────────────────────────────────────────

struct StreamSession {
    call_id: String,
    stream_id: String,
    ws_tx: tokio::sync::mpsc::UnboundedSender<Message>,
    incoming_tx: Sender<AudioFrame>,
    incoming_rx: Receiver<AudioFrame>,
    audio_buf: Arc<AudioBuffer>,  // Agent voice audio with backpressure
    bg_audio_buf: Arc<AudioBuffer>,  // Background audio (mixed in send loop)
    extra_headers: HashMap<String, String>,
    encoding: AudioEncoding,
    // Audio control flags (same as SIP RTP transport)
    muted: Arc<AtomicBool>,
    paused: Arc<AtomicBool>,
    playout_notify: Arc<(Mutex<bool>, Condvar)>,
    // Checkpoint tracking
    checkpoint_counter: AtomicU64,
    checkpoint_notify: Arc<(Mutex<Option<String>>, Condvar)>,
    cancel: CancellationToken,
}

// ─── AudioStreamEndpoint ─────────────────────────────────────────────────────

pub struct AudioStreamEndpoint {
    config: AudioStreamConfig,
    runtime: Runtime,
    sessions: Arc<Mutex<HashMap<String, StreamSession>>>,
    event_tx: Sender<EndpointEvent>,
    event_rx: Receiver<EndpointEvent>,
    cancel: CancellationToken,
}

impl AudioStreamEndpoint {
    pub fn new(config: AudioStreamConfig) -> Result<Self> {
        let rt = Runtime::new().map_err(|e| EndpointError::Other(e.to_string()))?;
        let (etx, erx) = crossbeam_channel::unbounded();
        let cancel = CancellationToken::new();
        let sessions = Arc::new(Mutex::new(HashMap::new()));

        let (addr, sess, etx2, cc) = (config.listen_addr.clone(), sessions.clone(), etx.clone(), cancel.clone());
        rt.spawn(async move {
            if let Err(e) = run_ws_server(&addr, sess, etx2, cc).await { error!("WS server: {}", e); }
        });

        info!("Audio streaming endpoint on {}", config.listen_addr);
        Ok(Self { config, runtime: rt, sessions, event_tx: etx, event_rx: erx, cancel })
    }

    /// Send audio with completion callback — matches SipEndpoint::send_audio_with_callback.
    ///
    /// If buffer is below threshold (200ms), the callback fires immediately.
    /// If above threshold, the callback is deferred until the send loop drains below threshold.
    /// This is the backpressure mechanism used by SipAudioSource in the Python/Node adapters.
    pub fn send_audio_with_callback(&self, session_id: &str, frame: &AudioFrame, on_complete: CompletionCallback) -> Result<()> {
        let audio_buf = {
            let s = self.sessions.lock().unwrap();
            let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
            sess.audio_buf.clone()
        };
        audio_buf.push(&frame.data, on_complete)
            .map_err(|e| EndpointError::Other(e.into()))
    }

    /// Send audio frame — simple, no backpressure callback.
    pub fn send_audio(&self, session_id: &str, frame: &AudioFrame) -> Result<()> {
        self.send_audio_with_callback(session_id, frame, Box::new(|| {}))
    }

    /// Send background audio to be mixed with agent voice in the send loop.
    /// Used by publish_track (background audio, hold music, etc.).
    /// No backpressure — background audio is best-effort.
    pub fn send_background_audio(&self, session_id: &str, frame: &AudioFrame) -> Result<()> {
        let bg_buf = {
            let s = self.sessions.lock().unwrap();
            let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
            sess.bg_audio_buf.clone()
        };
        bg_buf.push(&frame.data, Box::new(|| {}))
            .map_err(|e| EndpointError::Other(e.into()))
    }

    /// Mute outgoing audio (send silence, preserve queue).
    pub fn mute(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        sess.muted.store(true, Ordering::Relaxed); Ok(())
    }

    /// Unmute outgoing audio.
    pub fn unmute(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        sess.muted.store(false, Ordering::Relaxed); Ok(())
    }

    /// Pause audio playback — send loop outputs nothing, queue preserved.
    /// Frames accumulate in the channel during pause (same as LiveKit).
    pub fn pause(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        sess.paused.store(true, Ordering::Relaxed);
        Ok(())
    }

    /// Resume audio playback.
    pub fn resume(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        sess.paused.store(false, Ordering::Relaxed); Ok(())
    }

    pub fn recv_audio(&self, session_id: &str) -> Result<Option<AudioFrame>> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        Ok(sess.incoming_rx.try_recv().ok())
    }

    pub fn recv_audio_blocking(&self, session_id: &str, timeout_ms: u64) -> Result<Option<AudioFrame>> {
        let rx = { let s = self.sessions.lock().unwrap(); s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?.incoming_rx.clone() };
        Ok(rx.recv_timeout(std::time::Duration::from_millis(timeout_ms)).ok())
    }

    /// Clear buffered audio — drains local AudioBuffer AND sends clearAudio to Plivo.
    pub fn clear_buffer(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        // Clear local AudioBuffer (fires any pending completion callbacks)
        sess.audio_buf.clear();
        // Send clearAudio to Plivo to clear server-side buffer
        let json = serde_json::to_string(&serde_json::json!({ "event": "clearAudio", "streamId": sess.stream_id }))
            .map_err(|e| EndpointError::Other(e.to_string()))?;
        sess.ws_tx.send(Message::Text(json)).map_err(|_| EndpointError::Other("WS send failed".into()))
    }

    /// Send a checkpoint — Plivo will respond with playedStream when all audio up to this point has played.
    /// Returns the checkpoint name for tracking.
    pub fn checkpoint(&self, session_id: &str, name: Option<&str>) -> Result<String> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        let cp_name = name.map(String::from).unwrap_or_else(|| {
            format!("cp-{}", sess.checkpoint_counter.fetch_add(1, Ordering::Relaxed))
        });
        let json = serde_json::to_string(&serde_json::json!({ "event": "checkpoint", "streamId": sess.stream_id, "name": cp_name }))
            .map_err(|e| EndpointError::Other(e.to_string()))?;
        sess.ws_tx.send(Message::Text(json)).map_err(|_| EndpointError::Other("WS send failed".into()))?;
        debug!("Checkpoint '{}' sent for session {}", cp_name, session_id);
        Ok(cp_name)
    }

    /// Flush: send checkpoint and wait for playedStream confirmation.
    /// This is the audio streaming equivalent of SIP's flush + wait_for_playout.
    pub fn flush(&self, session_id: &str) -> Result<()> {
        let cp_name = self.checkpoint(session_id, None)?;
        debug!("Flush: waiting for checkpoint '{}' on session {}", cp_name, session_id);
        // Don't block here — just mark the checkpoint. wait_for_playout blocks.
        Ok(())
    }

    /// Wait for the last checkpoint to be confirmed by Plivo (playedStream event).
    /// Clears the checkpoint state after consuming so subsequent calls block correctly.
    pub fn wait_for_playout(&self, session_id: &str, timeout_ms: u64) -> Result<bool> {
        let notify = {
            let s = self.sessions.lock().unwrap();
            let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
            sess.checkpoint_notify.clone()
        };
        let (lock, cvar) = &*notify;
        let mut guard = lock.lock().unwrap();
        let (ref mut guard, timeout) = cvar.wait_timeout_while(guard, std::time::Duration::from_millis(timeout_ms), |cp| cp.is_none()).unwrap();
        // Clear checkpoint state after consuming — prevents stale data on next wait
        **guard = None;
        Ok(!timeout.timed_out())
    }

    /// Send DTMF digits via Plivo audio streaming.
    pub fn send_dtmf(&self, session_id: &str, digits: &str) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        let json = serde_json::to_string(&serde_json::json!({ "event": "sendDTMF", "dtmf": digits }))
            .map_err(|e| EndpointError::Other(e.to_string()))?;
        sess.ws_tx.send(Message::Text(json)).map_err(|_| EndpointError::Other("WS send failed".into()))?;
        info!("DTMF '{}' sent on session {}", digits, session_id);
        Ok(())
    }

    /// Hangup via Plivo REST API. Idempotent.
    pub fn hangup(&self, session_id: &str) -> Result<()> {
        let plivo_call_id = {
            let sess = self.sessions.lock().unwrap().remove(session_id);
            match sess { Some(s) => { s.cancel.cancel(); s.call_id.clone() }, None => return Ok(()) }
        };
        let (auth_id, auth_token) = (self.config.plivo_auth_id.clone(), self.config.plivo_auth_token.clone());
        if auth_id.is_empty() { return Ok(()); }
        self.runtime.block_on(async {
            let url = format!("https://api.plivo.com/v1/Account/{}/Call/{}/", auth_id, plivo_call_id);
            match reqwest::Client::new().delete(&url).basic_auth(&auth_id, Some(&auth_token)).send().await {
                Ok(r) if r.status().is_success() || r.status().as_u16() == 404 => info!("Call {} hung up", plivo_call_id),
                Ok(r) => warn!("Hangup: {} {}", r.status(), r.text().await.unwrap_or_default()),
                Err(e) => warn!("Hangup: {}", e),
            }
        });
        Ok(())
    }

    /// Send a raw text message over the WebSocket (e.g. for OutputTransportMessageFrame pass-through).
    pub fn send_raw_message(&self, session_id: &str, message: &str) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        sess.ws_tx.send(Message::Text(message.to_string())).map_err(|_| EndpointError::Other("WS send failed".into()))
    }

    /// Get the incoming audio receiver for async operations (used by Node.js async tasks).
    pub fn incoming_rx(&self, session_id: &str) -> Result<Receiver<AudioFrame>> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        Ok(sess.incoming_rx.clone())
    }

    /// Get the checkpoint notify handle for async operations (used by Node.js async tasks).
    pub fn checkpoint_notify(&self, session_id: &str) -> Result<Arc<(Mutex<Option<String>>, Condvar)>> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        Ok(sess.checkpoint_notify.clone())
    }

    pub fn queued_frames(&self, session_id: &str) -> Result<usize> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        Ok(sess.audio_buf.len())
    }
    pub fn sample_rate(&self) -> u32 { self.config.sample_rate }
    pub fn events(&self) -> Receiver<EndpointEvent> { self.event_rx.clone() }

    pub fn shutdown(&self) -> Result<()> {
        self.cancel.cancel();
        if self.config.auto_hangup {
            let ids: Vec<String> = self.sessions.lock().unwrap().keys().cloned().collect();
            for id in ids { let _ = self.hangup(&id); }
        }
        info!("Audio streaming shut down");
        Ok(())
    }
}

impl Drop for AudioStreamEndpoint { fn drop(&mut self) { let _ = self.shutdown(); } }

// ─── WebSocket server ────────────────────────────────────────────────────────

async fn run_ws_server(addr: &str, sessions: Arc<Mutex<HashMap<String, StreamSession>>>, etx: Sender<EndpointEvent>, cancel: CancellationToken) -> std::result::Result<(), Box<dyn std::error::Error>> {
    let listener = TcpListener::bind(addr).await?;
    loop {
        tokio::select! {
            _ = cancel.cancelled() => break,
            result = listener.accept() => {
                let (stream, peer) = result?;
                info!("WS connection from {}", peer);
                let ws = tokio_tungstenite::accept_async(stream).await?;
                let sid = format!("ws-{:016x}", rand::random::<u64>());
                let (s, e, c) = (sessions.clone(), etx.clone(), cancel.clone());
                tokio::spawn(async move { handle_ws(ws, sid, s, e, c).await; });
            }
        }
    }
    Ok(())
}

async fn handle_ws(ws: tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>, sid: String, sessions: Arc<Mutex<HashMap<String, StreamSession>>>, etx: Sender<EndpointEvent>, cancel: CancellationToken) {
    use futures_util::{SinkExt, StreamExt};
    let (mut sink, mut stream) = ws.split();
    let (ws_tx, mut ws_rx) = tokio::sync::mpsc::unbounded_channel::<Message>();
    let (itx, irx) = crossbeam_channel::unbounded(); // unbounded — matches WebRTC's FfiQueue

    let cc = cancel.clone();
    tokio::spawn(async move { loop { tokio::select! { _ = cc.cancelled() => break, msg = ws_rx.recv() => { match msg { Some(m) => { if sink.send(m).await.is_err() { break; } }, None => break } } } } });

    let mut encoding = AudioEncoding::MulawRate8k; // default, updated on start
    // speexdsp resampler for 8kHz→16kHz recv path (created on first use if needed)
    let mut upsampler: Option<crate::sip::resampler::Resampler> = None;

    loop {
        tokio::select! {
            _ = cancel.cancelled() => break,
            msg = stream.next() => {
                let msg = match msg { Some(Ok(Message::Text(t))) => t, Some(Ok(Message::Close(_))) | None => break, _ => continue };
                let plivo: PlivoMessage = match serde_json::from_str(&msg) { Ok(m) => m, Err(_) => continue };

                match plivo.event.as_str() {
                    "start" => {
                        if let Some(start) = plivo.start {
                            encoding = start.media_format.as_ref().map(|f| AudioEncoding::from_format(f)).unwrap_or(AudioEncoding::MulawRate8k);
                            // extra_headers is a TOP-LEVEL field per Plivo protocol
                            let mut headers = HashMap::new();
                            if let Some(ref h) = plivo.extra_headers {
                                if h.starts_with('{') {
                                    if let Ok(p) = serde_json::from_str::<HashMap<String, String>>(h) { headers = p; }
                                } else {
                                    // Plivo uses semicolon-delimited key=value pairs
                                    for part in h.split(';') {
                                        if let Some((k, v)) = part.split_once('=') { headers.insert(k.trim().to_string(), v.trim().to_string()); }
                                    }
                                }
                            }

                            // Create AudioBuffers + control flags (same pattern as SIP)
                            let audio_buf = Arc::new(AudioBuffer::new());
                            let bg_audio_buf = Arc::new(AudioBuffer::new()); // Background audio (hold music, ambient)
                            let muted = Arc::new(AtomicBool::new(false));
                            let paused = Arc::new(AtomicBool::new(false));
                            let playout_notify = Arc::new((Mutex::new(false), Condvar::new()));
                            let cp_notify = Arc::new((Mutex::new(None), Condvar::new()));
                            let session_cancel = CancellationToken::new();

                            // Spawn send loop — paces outgoing audio at 20ms intervals
                            // Mixes agent voice + background audio before encoding
                            let enc = encoding;
                            let wstx = ws_tx.clone();
                            let ab = audio_buf.clone();
                            let bg = bg_audio_buf.clone();
                            let (m, p, pn) = (muted.clone(), paused.clone(), playout_notify.clone());
                            let sc = session_cancel.clone();
                            let input_spf: usize = 320; // 16kHz × 20ms = 320 samples per frame
                            tokio::spawn(async move {
                                let mut iv = tokio::time::interval(Duration::from_millis(20));
                                // speexdsp resampler for 16kHz→8kHz (if needed for this encoding)
                                let mut downsampler = if enc.sample_rate() < 16000 {
                                    crate::sip::resampler::Resampler::new_voip(16000, enc.sample_rate())
                                } else { None };
                                loop {
                                    tokio::select! { _ = sc.cancelled() => break, _ = iv.tick() => {} }
                                    if p.load(Ordering::Relaxed) { continue; }

                                    // Drain agent voice + background audio
                                    let voice = ab.drain(input_spf);
                                    let bg_samples = bg.drain(input_spf);

                                    // Mix: add samples, clamp to i16 range
                                    let has_voice = !voice.is_empty();
                                    let has_bg = !bg_samples.is_empty();

                                    if has_voice || has_bg {
                                        let mixed = if has_voice && has_bg {
                                            // Mix voice + background
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

                                        if !m.load(Ordering::Relaxed) {
                                            let payload = encode_for_plivo(&mixed, enc, &mut downsampler);
                                            let b64 = base64::engine::general_purpose::STANDARD.encode(&payload);
                                            let json = serde_json::to_string(&serde_json::json!({
                                                "event": "playAudio",
                                                "media": { "contentType": enc.content_type(), "sampleRate": enc.send_sample_rate(), "payload": b64 }
                                            })).unwrap_or_default();
                                            let _ = wstx.send(Message::Text(json));
                                        }
                                    } else {
                                        // No audio — notify playout completion
                                        if ab.is_empty() {
                                            if let Ok(mut d) = pn.0.lock() { *d = true; pn.1.notify_all(); }
                                        }
                                    }
                                }
                            });

                            sessions.lock().unwrap().insert(sid.clone(), StreamSession {
                                call_id: start.call_id.clone(), stream_id: start.stream_id.clone(),
                                ws_tx: ws_tx.clone(), incoming_tx: itx.clone(), incoming_rx: irx.clone(),
                                audio_buf, bg_audio_buf, extra_headers: headers.clone(), encoding,
                                muted, paused, playout_notify,
                                checkpoint_counter: AtomicU64::new(0), checkpoint_notify: cp_notify,
                                cancel: session_cancel,
                            });

                            let mut session = crate::sip::call::CallSession::new(sid.clone(), crate::sip::call::CallDirection::Inbound);
                            session.call_uuid = Some(start.call_id.clone());
                            session.remote_uri = start.call_id;
                            session.local_uri = start.stream_id.clone();
                            session.extra_headers = headers;
                            let _ = etx.try_send(EndpointEvent::IncomingCall { session });
                            let _ = etx.try_send(EndpointEvent::CallMediaActive { call_id: sid.clone() });
                            info!("Session {} started (encoding={:?})", sid, encoding);
                        }
                    }
                    "media" => {
                        if let Some(media) = plivo.media {
                            if let Ok(raw) = base64::engine::general_purpose::STANDARD.decode(&media.payload) {
                                let pcm_16k = match encoding {
                                    AudioEncoding::L16Rate16k => {
                                        raw.chunks_exact(2).map(|c| i16::from_le_bytes([c[0], c[1]])).collect::<Vec<_>>()
                                    }
                                    AudioEncoding::L16Rate8k => {
                                        let pcm_8k: Vec<i16> = raw.chunks_exact(2).map(|c| i16::from_le_bytes([c[0], c[1]])).collect();
                                        if upsampler.is_none() { upsampler = crate::sip::resampler::Resampler::new_voip(8000, 16000); }
                                        if let Some(ref mut us) = upsampler { us.process(&pcm_8k).to_vec() } else { pcm_8k }
                                    }
                                    AudioEncoding::MulawRate8k => {
                                        let pcm_8k: Vec<i16> = raw.iter().map(|&b| decode_ulaw(b)).collect();
                                        if upsampler.is_none() { upsampler = crate::sip::resampler::Resampler::new_voip(8000, 16000); }
                                        if let Some(ref mut us) = upsampler { us.process(&pcm_8k).to_vec() } else { pcm_8k }
                                    }
                                };
                                let n = pcm_16k.len() as u32;
                                let _ = itx.try_send(AudioFrame { data: pcm_16k, sample_rate: 16000, num_channels: 1, samples_per_channel: n });
                            }
                        }
                    }
                    "dtmf" => {
                        if let Some(dtmf) = plivo.dtmf {
                            if let Some(d) = dtmf.digit.chars().next() {
                                let _ = etx.try_send(EndpointEvent::DtmfReceived { call_id: sid.clone(), digit: d, method: "plivo_ws".into() });
                            }
                        }
                    }
                    "playedStream" => {
                        // Checkpoint confirmation — audio up to this checkpoint has played
                        if let Some(name) = plivo.name {
                            debug!("playedStream: checkpoint '{}' on session {}", name, sid);
                            if let Some(sess) = sessions.lock().unwrap().get(sid.as_str()) {
                                let (lock, cvar) = &*sess.checkpoint_notify;
                                *lock.lock().unwrap() = Some(name);
                                cvar.notify_all();
                            }
                        }
                    }
                    "clearedAudio" => {
                        debug!("clearedAudio confirmed on session {}", sid);
                        // Could emit an event here if needed
                    }
                    "stop" => {
                        info!("Session {} stopped", sid);
                        if let Some(sess) = sessions.lock().unwrap().remove(&sid) {
                            sess.cancel.cancel(); // Stop the send loop task
                            let session = crate::sip::call::CallSession::new(sid.clone(), crate::sip::call::CallDirection::Inbound);
                            let _ = etx.try_send(EndpointEvent::CallTerminated { session, reason: "stream stopped".into() });
                        }
                        break;
                    }
                    _ => {}
                }
            }
        }
    }

    // Cleanup on WS disconnect (handles abrupt disconnects without "stop" event)
    // If session still exists, it means we broke out of the loop without a "stop" event
    // (e.g., caller hung up, network drop, Plivo closed WS without sending "stop")
    if let Some(sess) = sessions.lock().unwrap().remove(&sid) {
        sess.cancel.cancel();
        let session = crate::sip::call::CallSession::new(sid.clone(), crate::sip::call::CallDirection::Inbound);
        let _ = etx.try_send(EndpointEvent::CallTerminated { session, reason: "ws disconnected".into() });
        info!("Session {} cleaned up (WS disconnected)", sid);
    }
}

/// Encode 16kHz PCM samples for Plivo based on negotiated format.
/// Uses speexdsp resampler when downsampling is needed.
fn encode_for_plivo(samples: &[i16], enc: AudioEncoding, downsampler: &mut Option<crate::sip::resampler::Resampler>) -> Vec<u8> {
    match enc {
        AudioEncoding::L16Rate16k => {
            samples.iter().flat_map(|&s| s.to_le_bytes()).collect()
        }
        AudioEncoding::L16Rate8k => {
            let pcm_8k = if let Some(ref mut ds) = downsampler {
                ds.process(samples).to_vec()
            } else { samples.to_vec() };
            pcm_8k.iter().flat_map(|&s| s.to_le_bytes()).collect()
        }
        AudioEncoding::MulawRate8k => {
            let pcm_8k = if let Some(ref mut ds) = downsampler {
                ds.process(samples).to_vec()
            } else { samples.to_vec() };
            pcm_8k.iter().map(|&s| encode_ulaw(s)).collect()
        }
    }
}
