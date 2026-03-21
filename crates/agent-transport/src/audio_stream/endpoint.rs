//! Plivo WebSocket audio streaming endpoint.
//!
//! Accepts incoming WebSocket connections from Plivo's audio streaming service.
//! Receives mu-law audio over JSON WebSocket messages, decodes to PCM int16 at 16kHz.
//! Sends PCM audio back encoded as mu-law base64 over WebSocket.

use std::collections::HashMap;
use std::sync::atomic::{AtomicI32, Ordering};
use std::sync::{Arc, Mutex};

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

// ─── Plivo WebSocket message types ───────────────────────────────────────────

#[derive(Deserialize)]
struct PlivoMessage {
    event: String,
    #[serde(default)]
    start: Option<PlivoStart>,
    #[serde(default)]
    media: Option<PlivoMedia>,
    #[serde(default)]
    dtmf: Option<PlivoDtmf>,
    #[serde(rename = "streamId", default)]
    stream_id: Option<String>,
}

#[derive(Deserialize)]
struct PlivoStart {
    #[serde(rename = "callId")]
    call_id: String,
    #[serde(rename = "streamId")]
    stream_id: String,
    #[serde(default, rename = "extra_headers")]
    extra_headers: Option<String>,
}

#[derive(Deserialize)]
struct PlivoMedia {
    payload: String, // base64-encoded mu-law
}

#[derive(Deserialize)]
struct PlivoDtmf {
    digit: String,
}

#[derive(Serialize)]
struct PlayAudioMessage {
    event: &'static str,
    media: PlayAudioMedia,
}

#[derive(Serialize)]
struct PlayAudioMedia {
    #[serde(rename = "contentType")]
    content_type: &'static str,
    #[serde(rename = "sampleRate")]
    sample_rate: u32,
    payload: String, // base64-encoded mu-law
}

#[derive(Serialize)]
struct ClearAudioMessage {
    event: &'static str,
    #[serde(rename = "streamId")]
    stream_id: String,
}

// ─── Session context ─────────────────────────────────────────────────────────

struct StreamSession {
    call_id: String,
    stream_id: String,
    ws_tx: tokio::sync::mpsc::UnboundedSender<Message>,
    incoming_tx: Sender<AudioFrame>,
    incoming_rx: Receiver<AudioFrame>,
    extra_headers: HashMap<String, String>,
}

// ─── AudioStreamEndpoint ─────────────────────────────────────────────────────

/// Plivo WebSocket audio streaming endpoint.
///
/// Listens for WebSocket connections from Plivo's audio streaming service.
/// Each connection represents one call session.
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

        // Start WebSocket server
        let (addr, sess, etx2, cc) = (config.listen_addr.clone(), sessions.clone(), etx.clone(), cancel.clone());
        rt.spawn(async move {
            if let Err(e) = run_ws_server(&addr, sess, etx2, cc).await {
                error!("WebSocket server error: {}", e);
            }
        });

        info!("Audio streaming endpoint listening on {}", config.listen_addr);
        Ok(Self { config, runtime: rt, sessions, next_id: AtomicI32::new(0), event_tx: etx, event_rx: erx, cancel })
    }

    /// Send audio frame to a session.
    pub fn send_audio(&self, session_id: i32, frame: &AudioFrame) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;

        // Downsample 16kHz → 8kHz, encode to mu-law, base64
        let pcm_8k: Vec<i16> = frame.data.iter().step_by(2).copied().collect();
        let ulaw: Vec<u8> = pcm_8k.iter().map(|&s| encode_ulaw(s)).collect();
        let b64 = base64::engine::general_purpose::STANDARD.encode(&ulaw);

        let msg = PlayAudioMessage {
            event: "playAudio",
            media: PlayAudioMedia { content_type: "audio/x-mulaw", sample_rate: 8000, payload: b64 },
        };
        let json = serde_json::to_string(&msg).map_err(|e| EndpointError::Other(e.to_string()))?;
        sess.ws_tx.send(Message::Text(json)).map_err(|_| EndpointError::Other("WebSocket send failed".into()))?;
        Ok(())
    }

    /// Receive next audio frame (non-blocking).
    pub fn recv_audio(&self, session_id: i32) -> Result<Option<AudioFrame>> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;
        Ok(sess.incoming_rx.try_recv().ok())
    }

    /// Receive audio frame, blocking until available or timeout.
    /// Avoids Python polling loops that cause jitter at high concurrency.
    pub fn recv_audio_blocking(&self, session_id: i32, timeout_ms: u64) -> Result<Option<AudioFrame>> {
        let rx = {
            let s = self.sessions.lock().unwrap();
            let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;
            sess.incoming_rx.clone()
        };
        Ok(rx.recv_timeout(std::time::Duration::from_millis(timeout_ms)).ok())
    }

    /// Clear buffered audio (send clearAudio to Plivo).
    pub fn clear_buffer(&self, session_id: i32) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;
        let msg = ClearAudioMessage { event: "clearAudio", stream_id: sess.stream_id.clone() };
        let json = serde_json::to_string(&msg).map_err(|e| EndpointError::Other(e.to_string()))?;
        sess.ws_tx.send(Message::Text(json)).map_err(|_| EndpointError::Other("WebSocket send failed".into()))?;
        debug!("clearAudio sent for session {}", session_id);
        Ok(())
    }

    /// Hang up the call via Plivo REST API.
    pub fn hangup(&self, session_id: i32) -> Result<()> {
        let call_id = {
            let s = self.sessions.lock().unwrap();
            let sess = s.get(&session_id).ok_or(EndpointError::CallNotActive(session_id))?;
            sess.call_id.clone()
        };

        let (auth_id, auth_token) = (self.config.plivo_auth_id.clone(), self.config.plivo_auth_token.clone());
        self.runtime.block_on(async {
            let url = format!("https://api.plivo.com/v1/Account/{}/Call/{}/", auth_id, call_id);
            let client = reqwest::Client::new();
            match client.delete(&url).basic_auth(&auth_id, Some(&auth_token)).send().await {
                Ok(r) if r.status().is_success() || r.status().as_u16() == 404 => { info!("Call {} hung up", call_id); }
                Ok(r) => { warn!("Hangup failed: {}", r.status()); }
                Err(e) => { warn!("Hangup error: {}", e); }
            }
        });

        self.sessions.lock().unwrap().remove(&session_id);
        Ok(())
    }

    /// Number of queued outgoing frames.
    pub fn queued_frames(&self, _session_id: i32) -> Result<usize> {
        Ok(0) // Audio streaming is fire-and-forget, no outgoing queue
    }

    /// Audio sample rate.
    pub fn sample_rate(&self) -> u32 { self.config.sample_rate }

    /// Get event receiver.
    pub fn events(&self) -> Receiver<EndpointEvent> { self.event_rx.clone() }

    /// Shut down.
    pub fn shutdown(&self) -> Result<()> {
        self.cancel.cancel();
        if self.config.auto_hangup {
            let ids: Vec<i32> = self.sessions.lock().unwrap().keys().copied().collect();
            for id in ids { let _ = self.hangup(id); }
        }
        info!("Audio streaming endpoint shut down");
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
                info!("WebSocket connection from {}", peer);
                let ws = tokio_tungstenite::accept_async(stream).await?;
                let sid = next_id.fetch_add(1, Ordering::Relaxed);
                let (sess_clone, etx_clone, cc) = (sessions.clone(), etx.clone(), cancel.clone());
                tokio::spawn(async move {
                    handle_ws_session(ws, sid, sess_clone, etx_clone, cc).await;
                });
            }
        }
    }
    Ok(())
}

async fn handle_ws_session(ws: tokio_tungstenite::WebSocketStream<tokio::net::TcpStream>, session_id: i32, sessions: Arc<Mutex<HashMap<i32, StreamSession>>>, etx: Sender<EndpointEvent>, cancel: CancellationToken) {
    use futures_util::{SinkExt, StreamExt};

    let (mut ws_sink, mut ws_stream) = ws.split();
    let (ws_tx, mut ws_rx) = tokio::sync::mpsc::unbounded_channel::<Message>();
    let (incoming_tx, incoming_rx) = crossbeam_channel::bounded(100);

    // Forward outgoing messages to WebSocket
    let cc = cancel.clone();
    tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = cc.cancelled() => break,
                msg = ws_rx.recv() => {
                    match msg {
                        Some(m) => { if ws_sink.send(m).await.is_err() { break; } }
                        None => break,
                    }
                }
            }
        }
    });

    // Process incoming messages
    loop {
        tokio::select! {
            _ = cancel.cancelled() => break,
            msg = ws_stream.next() => {
                let msg = match msg {
                    Some(Ok(Message::Text(t))) => t,
                    Some(Ok(Message::Close(_))) | None => break,
                    _ => continue,
                };

                let plivo: PlivoMessage = match serde_json::from_str(&msg) {
                    Ok(m) => m,
                    Err(_) => continue,
                };

                match plivo.event.as_str() {
                    "start" => {
                        if let Some(start) = plivo.start {
                            let mut headers = HashMap::new();
                            if let Some(ref h) = start.extra_headers {
                                if let Ok(parsed) = serde_json::from_str::<HashMap<String, String>>(h) {
                                    headers = parsed;
                                }
                            }
                            sessions.lock().unwrap().insert(session_id, StreamSession {
                                call_id: start.call_id.clone(),
                                stream_id: start.stream_id.clone(),
                                ws_tx: ws_tx.clone(),
                                incoming_tx: incoming_tx.clone(),
                                incoming_rx: incoming_rx.clone(),
                                extra_headers: headers.clone(),
                            });

                            let mut session = crate::sip::call::CallSession::new(session_id, crate::sip::call::CallDirection::Inbound);
                            session.remote_uri = start.call_id;
                            session.extra_headers = headers;
                            let _ = etx.try_send(EndpointEvent::IncomingCall { session });
                            let _ = etx.try_send(EndpointEvent::CallMediaActive { call_id: session_id });
                            info!("Audio stream session {} started (stream={})", session_id, start.stream_id);
                        }
                    }
                    "media" => {
                        if let Some(media) = plivo.media {
                            if let Ok(ulaw_bytes) = base64::engine::general_purpose::STANDARD.decode(&media.payload) {
                                // Decode mu-law → PCM 8kHz → upsample to 16kHz
                                let pcm_8k: Vec<i16> = ulaw_bytes.iter().map(|&b| decode_ulaw(b)).collect();
                                let mut pcm_16k = Vec::with_capacity(pcm_8k.len() * 2);
                                for i in 0..pcm_8k.len() {
                                    pcm_16k.push(pcm_8k[i]);
                                    let n = if i + 1 < pcm_8k.len() { pcm_8k[i + 1] } else { pcm_8k[i] };
                                    pcm_16k.push(((pcm_8k[i] as i32 + n as i32) / 2) as i16);
                                }
                                let n = pcm_16k.len() as u32;
                                let _ = incoming_tx.try_send(AudioFrame { data: pcm_16k, sample_rate: 16000, num_channels: 1, samples_per_channel: n });
                            }
                        }
                    }
                    "dtmf" => {
                        if let Some(dtmf) = plivo.dtmf {
                            if let Some(digit) = dtmf.digit.chars().next() {
                                let _ = etx.try_send(EndpointEvent::DtmfReceived { call_id: session_id, digit, method: "plivo_ws".into() });
                            }
                        }
                    }
                    "stop" => {
                        info!("Audio stream session {} stopped", session_id);
                        let sess = sessions.lock().unwrap().remove(&session_id);
                        if let Some(_) = sess {
                            let session = crate::sip::call::CallSession::new(session_id, crate::sip::call::CallDirection::Inbound);
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
