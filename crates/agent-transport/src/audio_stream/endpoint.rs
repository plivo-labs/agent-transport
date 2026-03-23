//! Plivo WebSocket audio streaming endpoint.
//!
//! Supports all 3 audio formats: x-l16 8kHz, x-l16 16kHz, x-mulaw 8kHz.
//! Handles: playAudio, clearAudio, checkpoint, sendDTMF commands.
//! Receives: start, media, dtmf, stop, playedStream, clearedAudio events.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use audio_codec_algorithms::{decode_ulaw, encode_ulaw};
use base64::Engine;
use crossbeam_channel::{Receiver, Sender};
use serde::{Deserialize, Serialize};
use tokio::net::TcpListener;
use tokio::runtime::Runtime;
use tokio_tungstenite::tungstenite::Message;
use tokio_util::sync::CancellationToken;
use tracing::{debug, error, info, warn};

use crate::audio::AudioFrame;
use crate::error::{EndpointError, Result};
use crate::events::EndpointEvent;
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
}

#[derive(Deserialize)]
struct PlivoStart {
    #[serde(rename = "callId")] call_id: String,
    #[serde(rename = "streamId")] stream_id: String,
    #[serde(default, rename = "extra_headers")] extra_headers: Option<String>,
    #[serde(default, rename = "mediaFormat")] media_format: Option<PlivoMediaFormat>,
}

#[derive(Deserialize, Clone)]
struct PlivoMediaFormat {
    #[serde(default)] encoding: String,
    #[serde(rename = "sampleRate", default)] sample_rate: u32,
}

#[derive(Deserialize)]
struct PlivoMedia { payload: String }

#[derive(Deserialize)]
struct PlivoDtmf { digit: String }

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
    outgoing_tx: Sender<Vec<i16>>,  // Outgoing audio queue (same as SIP)
    extra_headers: HashMap<String, String>,
    encoding: AudioEncoding,
    // Audio control flags (same as SIP RTP transport)
    muted: Arc<AtomicBool>,
    paused: Arc<AtomicBool>,
    flush_flag: Arc<AtomicBool>,
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
    sessions: Arc<Mutex<HashMap<i32, StreamSession>>>,
    next_id: AtomicI32,
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
        Ok(Self { config, runtime: rt, sessions, next_id: AtomicI32::new(0), event_tx: etx, event_rx: erx, cancel })
    }

    /// Send audio frame — queued and paced by Rust send loop (same as SIP).
    /// During pause, frames accumulate in the channel (same as LiveKit).
    pub fn send_audio(&self, session_id: i32, frame: &AudioFrame) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;
        sess.outgoing_tx.try_send(frame.data.clone())
            .map_err(|_| EndpointError::Other("audio buffer full".into()))
    }

    /// Mute outgoing audio (send silence, preserve queue).
    pub fn mute(&self, session_id: i32) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;
        sess.muted.store(true, Ordering::Relaxed); Ok(())
    }

    /// Unmute outgoing audio.
    pub fn unmute(&self, session_id: i32) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;
        sess.muted.store(false, Ordering::Relaxed); Ok(())
    }

    /// Pause audio playback — send loop outputs nothing, queue preserved.
    /// Frames accumulate in the channel during pause (same as LiveKit).
    pub fn pause(&self, session_id: i32) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;
        sess.paused.store(true, Ordering::Relaxed);
        Ok(())
    }

    /// Resume audio playback.
    pub fn resume(&self, session_id: i32) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;
        sess.paused.store(false, Ordering::Relaxed); Ok(())
    }

    pub fn recv_audio(&self, session_id: i32) -> Result<Option<AudioFrame>> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;
        Ok(sess.incoming_rx.try_recv().ok())
    }

    pub fn recv_audio_blocking(&self, session_id: i32, timeout_ms: u64) -> Result<Option<AudioFrame>> {
        let rx = { let s = self.sessions.lock().unwrap(); s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?.incoming_rx.clone() };
        Ok(rx.recv_timeout(std::time::Duration::from_millis(timeout_ms)).ok())
    }

    /// Clear buffered audio — drains local queue AND sends clearAudio to Plivo.
    pub fn clear_buffer(&self, session_id: i32) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;
        // Drain local queue (same as SIP flush_flag)
        sess.flush_flag.store(true, Ordering::Relaxed);
        // Send clearAudio to Plivo to clear server-side buffer
        let json = serde_json::to_string(&serde_json::json!({ "event": "clearAudio", "streamId": sess.stream_id }))
            .map_err(|e| EndpointError::Other(e.to_string()))?;
        sess.ws_tx.send(Message::Text(json)).map_err(|_| EndpointError::Other("WS send failed".into()))
    }

    /// Send a checkpoint — Plivo will respond with playedStream when all audio up to this point has played.
    /// Returns the checkpoint name for tracking.
    pub fn checkpoint(&self, session_id: i32, name: Option<&str>) -> Result<String> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;
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
    pub fn flush(&self, session_id: i32) -> Result<()> {
        let cp_name = self.checkpoint(session_id, None)?;
        debug!("Flush: waiting for checkpoint '{}' on session {}", cp_name, session_id);
        // Don't block here — just mark the checkpoint. wait_for_playout blocks.
        Ok(())
    }

    /// Wait for the last checkpoint to be confirmed by Plivo (playedStream event).
    pub fn wait_for_playout(&self, session_id: i32, timeout_ms: u64) -> Result<bool> {
        let notify = {
            let s = self.sessions.lock().unwrap();
            let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;
            sess.checkpoint_notify.clone()
        };
        let (lock, cvar) = &*notify;
        let guard = lock.lock().unwrap();
        let result = cvar.wait_timeout_while(guard, std::time::Duration::from_millis(timeout_ms), |cp| cp.is_none()).unwrap();
        Ok(!result.1.timed_out())
    }

    /// Send DTMF digits via Plivo audio streaming.
    pub fn send_dtmf(&self, session_id: i32, digits: &str) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;
        let json = serde_json::to_string(&serde_json::json!({ "event": "sendDTMF", "dtmf": digits }))
            .map_err(|e| EndpointError::Other(e.to_string()))?;
        sess.ws_tx.send(Message::Text(json)).map_err(|_| EndpointError::Other("WS send failed".into()))?;
        info!("DTMF '{}' sent on session {}", digits, session_id);
        Ok(())
    }

    /// Hangup via Plivo REST API. Idempotent.
    pub fn hangup(&self, session_id: i32) -> Result<()> {
        let call_id = {
            let sess = self.sessions.lock().unwrap().remove(&session_id);
            match sess { Some(s) => { s.cancel.cancel(); s.call_id }, None => return Ok(()) }
        };
        let (auth_id, auth_token) = (self.config.plivo_auth_id.clone(), self.config.plivo_auth_token.clone());
        if auth_id.is_empty() { return Ok(()); }
        self.runtime.block_on(async {
            let url = format!("https://api.plivo.com/v1/Account/{}/Call/{}/", auth_id, call_id);
            match reqwest::Client::new().delete(&url).basic_auth(&auth_id, Some(&auth_token)).send().await {
                Ok(r) if r.status().is_success() || r.status().as_u16() == 404 => info!("Call {} hung up", call_id),
                Ok(r) => warn!("Hangup: {} {}", r.status(), r.text().await.unwrap_or_default()),
                Err(e) => warn!("Hangup: {}", e),
            }
        });
        Ok(())
    }

    /// Send a raw text message over the WebSocket (e.g. for OutputTransportMessageFrame pass-through).
    pub fn send_raw_message(&self, session_id: i32, message: &str) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;
        sess.ws_tx.send(Message::Text(message.to_string())).map_err(|_| EndpointError::Other("WS send failed".into()))
    }

    /// Get the incoming audio receiver for async operations (used by Node.js async tasks).
    pub fn incoming_rx(&self, session_id: i32) -> Result<Receiver<AudioFrame>> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;
        Ok(sess.incoming_rx.clone())
    }

    /// Get the checkpoint notify handle for async operations (used by Node.js async tasks).
    pub fn checkpoint_notify(&self, session_id: i32) -> Result<Arc<(Mutex<Option<String>>, Condvar)>> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;
        Ok(sess.checkpoint_notify.clone())
    }

    pub fn queued_frames(&self, session_id: i32) -> Result<usize> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;
        Ok(sess.outgoing_tx.len())
    }
    pub fn sample_rate(&self) -> u32 { self.config.sample_rate }
    pub fn events(&self) -> Receiver<EndpointEvent> { self.event_rx.clone() }

    pub fn shutdown(&self) -> Result<()> {
        self.cancel.cancel();
        if self.config.auto_hangup {
            let ids: Vec<i32> = self.sessions.lock().unwrap().keys().copied().collect();
            for id in ids { let _ = self.hangup(id); }
        }
        info!("Audio streaming shut down");
        Ok(())
    }
}

impl Drop for AudioStreamEndpoint { fn drop(&mut self) { let _ = self.shutdown(); } }

// ─── WebSocket server ────────────────────────────────────────────────────────

async fn run_ws_server(addr: &str, sessions: Arc<Mutex<HashMap<i32, StreamSession>>>, etx: Sender<EndpointEvent>, cancel: CancellationToken) -> std::result::Result<(), Box<dyn std::error::Error>> {
    let listener = TcpListener::bind(addr).await?;
    let next_id = AtomicI32::new(0);
    loop {
        tokio::select! {
            _ = cancel.cancelled() => break,
            result = listener.accept() => {
                let (stream, peer) = result?;
                info!("WS connection from {}", peer);
                let ws = tokio_tungstenite::accept_async(stream).await?;
                let sid = next_id.fetch_add(1, Ordering::Relaxed);
                let (s, e, c) = (sessions.clone(), etx.clone(), cancel.clone());
                tokio::spawn(async move { handle_ws(ws, sid, s, e, c).await; });
            }
        }
    }
    Ok(())
}

async fn handle_ws(ws: tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>, sid: i32, sessions: Arc<Mutex<HashMap<i32, StreamSession>>>, etx: Sender<EndpointEvent>, cancel: CancellationToken) {
    use futures_util::{SinkExt, StreamExt};
    let (mut sink, mut stream) = ws.split();
    let (ws_tx, mut ws_rx) = tokio::sync::mpsc::unbounded_channel::<Message>();
    let (itx, irx) = crossbeam_channel::bounded(18000);

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
                            let mut headers = HashMap::new();
                            if let Some(ref h) = start.extra_headers {
                                if h.starts_with('{') {
                                    if let Ok(p) = serde_json::from_str::<HashMap<String, String>>(h) { headers = p; }
                                } else {
                                    for part in h.split(',') {
                                        if let Some((k, v)) = part.split_once('=') { headers.insert(k.trim().to_string(), v.trim().to_string()); }
                                    }
                                }
                            }

                            // Create outgoing queue + control flags (same as SIP)
                            let (otx, orx): (Sender<Vec<i16>>, Receiver<Vec<i16>>) = crossbeam_channel::bounded(18000);
                            let muted = Arc::new(AtomicBool::new(false));
                            let paused = Arc::new(AtomicBool::new(false));
                            let flush_flag = Arc::new(AtomicBool::new(false));
                            let playout_notify = Arc::new((Mutex::new(false), Condvar::new()));
                            let cp_notify = Arc::new((Mutex::new(None), Condvar::new()));
                            let session_cancel = CancellationToken::new();

                            // Spawn send loop — paces outgoing audio at 20ms intervals
                            let enc = encoding;
                            let wstx = ws_tx.clone();
                            let (m, p, f, pn) = (muted.clone(), paused.clone(), flush_flag.clone(), playout_notify.clone());
                            let sc = session_cancel.clone();
                            tokio::spawn(async move {
                                let mut iv = tokio::time::interval(Duration::from_millis(20));
                                // speexdsp resampler for 16kHz→8kHz (if needed for this encoding)
                                let mut downsampler = if enc.sample_rate() < 16000 {
                                    crate::sip::resampler::Resampler::new_voip(16000, enc.sample_rate())
                                } else { None };
                                loop {
                                    tokio::select! { _ = sc.cancelled() => break, _ = iv.tick() => {} }
                                    if f.swap(false, Ordering::Relaxed) {
                                        while orx.try_recv().is_ok() {}
                                        if let Ok(mut d) = pn.0.lock() { *d = true; pn.1.notify_all(); }
                                        continue;
                                    }
                                    if p.load(Ordering::Relaxed) { continue; }
                                    match orx.try_recv() {
                                        Ok(samples) if !m.load(Ordering::Relaxed) => {
                                            let payload = encode_for_plivo(&samples, enc, &mut downsampler);
                                            let b64 = base64::engine::general_purpose::STANDARD.encode(&payload);
                                            let json = serde_json::to_string(&serde_json::json!({
                                                "event": "playAudio",
                                                "media": { "contentType": enc.content_type(), "sampleRate": enc.send_sample_rate(), "payload": b64 }
                                            })).unwrap_or_default();
                                            let _ = wstx.send(Message::Text(json));
                                        }
                                        Ok(_) => {} // Muted — consume frame but don't send
                                        Err(_) => { if let Ok(mut d) = pn.0.lock() { *d = true; pn.1.notify_all(); } } // Queue empty
                                    }
                                }
                            });

                            sessions.lock().unwrap().insert(sid, StreamSession {
                                call_id: start.call_id.clone(), stream_id: start.stream_id.clone(),
                                ws_tx: ws_tx.clone(), incoming_tx: itx.clone(), incoming_rx: irx.clone(),
                                outgoing_tx: otx, extra_headers: headers.clone(), encoding,
                                muted, paused, flush_flag, playout_notify,
                                checkpoint_counter: AtomicU64::new(0), checkpoint_notify: cp_notify,
                                cancel: session_cancel,
                            });

                            let mut session = crate::sip::call::CallSession::new(sid, crate::sip::call::CallDirection::Inbound);
                            session.remote_uri = start.call_id;
                            session.extra_headers = headers;
                            let _ = etx.try_send(EndpointEvent::IncomingCall { session });
                            let _ = etx.try_send(EndpointEvent::CallMediaActive { call_id: sid });
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
                                let _ = etx.try_send(EndpointEvent::DtmfReceived { call_id: sid, digit: d, method: "plivo_ws".into() });
                            }
                        }
                    }
                    "playedStream" => {
                        // Checkpoint confirmation — audio up to this checkpoint has played
                        if let Some(name) = plivo.name {
                            debug!("playedStream: checkpoint '{}' on session {}", name, sid);
                            if let Some(sess) = sessions.lock().unwrap().get(&sid) {
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
                            let session = crate::sip::call::CallSession::new(sid, crate::sip::call::CallDirection::Inbound);
                            let _ = etx.try_send(EndpointEvent::CallTerminated { session, reason: "stream stopped".into() });
                        }
                        break;
                    }
                    _ => {}
                }
            }
        }
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
