//! Generic WebSocket audio streaming endpoint.
//!
//! Provider-agnostic: all protocol specifics (message parsing, audio encoding,
//! hangup API) are delegated to a `StreamProtocol` implementation.
//!
//! This module handles: WebSocket server, session management, audio mixing,
//! resampling (speexdsp), checkpoint-paced send loop, recording, beep detection.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use crossbeam_channel::{Receiver, Sender};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::TcpListener;
use tokio::runtime::Runtime;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::WebSocketStream;
use tokio_util::sync::CancellationToken;
use tracing::{debug, error, info, warn};

use beep_detector::{BeepDetector, BeepDetectorConfig, BeepDetectorResult};
use crate::audio::AudioFrame;
use crate::error::{EndpointError, Result};
use crate::events::EndpointEvent;
use crate::sip::audio_buffer::{AudioBuffer, CompletionCallback};
use super::config::AudioStreamConfig;
use super::protocol::{StreamEvent, StreamProtocol, WireEncoding};

// ─── Session context ─────────────────────────────────────────────────────────

struct StreamSession {
    call_id: String,
    stream_id: String,
    ws_tx: tokio::sync::mpsc::UnboundedSender<Message>,
    incoming_tx: Sender<AudioFrame>,
    incoming_rx: Receiver<AudioFrame>,
    audio_buf: Arc<AudioBuffer>,
    bg_audio_buf: Arc<AudioBuffer>,
    extra_headers: HashMap<String, String>,
    encoding: WireEncoding,
    muted: Arc<AtomicBool>,
    paused: Arc<AtomicBool>,
    playout_notify: Arc<(Mutex<bool>, Condvar)>,
    checkpoint_counter: AtomicU64,
    checkpoint_notify: Arc<(Mutex<Option<String>>, Condvar)>,
    send_loop_notify: Arc<tokio::sync::Notify>,
    /// Checkpoint name queued by flush() — send loop sends it when buffer empties.
    /// Ensures checkpoint is ordered AFTER all playAudio messages.
    pending_flush: Arc<Mutex<Option<String>>>,
    /// The checkpoint name we're currently waiting for Plivo to confirm.
    /// Set when the send loop sends the checkpoint, cleared on confirm or clear_buffer.
    /// CheckpointAck only notifies condvar if the name matches, preventing stale acks.
    awaiting_checkpoint: Arc<Mutex<Option<String>>>,
    recorder: Arc<Mutex<Option<Arc<crate::recorder::CallRecorder>>>>,
    beep_detector: Arc<Mutex<Option<BeepDetector>>>,
    cancel: CancellationToken,
}

// ─── AudioStreamEndpoint ─────────────────────────────────────────────────────

pub struct AudioStreamEndpoint {
    config: AudioStreamConfig,
    protocol: Arc<dyn StreamProtocol>,
    runtime: Runtime,
    sessions: Arc<Mutex<HashMap<String, StreamSession>>>,
    event_tx: Sender<EndpointEvent>,
    event_rx: Receiver<EndpointEvent>,
    cancel: CancellationToken,
    recording_mgr: Arc<crate::recorder::RecordingManager>,
}

impl AudioStreamEndpoint {
    pub fn new(config: AudioStreamConfig, protocol: Arc<dyn StreamProtocol>) -> Result<Self> {
        if config.input_sample_rate == 0 || config.output_sample_rate == 0 { return Err(EndpointError::Other("sample_rate must be > 0".into())); }
        let rt = Runtime::new().map_err(|e| EndpointError::Other(e.to_string()))?;
        let (etx, erx) = crossbeam_channel::unbounded();
        let cancel = CancellationToken::new();
        let sessions = Arc::new(Mutex::new(HashMap::new()));

        // Load TLS config if cert/key paths provided
        let tls_acceptor = match (&config.tls_cert_path, &config.tls_key_path) {
            (Some(cert_path), Some(key_path)) => {
                let acceptor = load_tls_acceptor(cert_path, key_path)
                    .map_err(|e| EndpointError::Other(format!("TLS config error: {}", e)))?;
                info!("TLS enabled (cert={}, key={})", cert_path, key_path);
                Some(acceptor)
            }
            (Some(_), None) | (None, Some(_)) => {
                return Err(EndpointError::Other("Both tls_cert_path and tls_key_path are required for TLS".into()));
            }
            _ => None,
        };

        let recording_mgr = crate::recorder::RecordingManager::new();

        let (addr, sess, etx2, cc, isr, osr, proto, rmgr) = (
            config.listen_addr.clone(), sessions.clone(), etx.clone(),
            cancel.clone(), config.input_sample_rate, config.output_sample_rate, protocol.clone(),
            recording_mgr.clone(),
        );
        rt.spawn(async move {
            if let Err(e) = run_ws_server(&addr, sess, etx2, cc, isr, osr, proto, tls_acceptor, rmgr).await {
                error!("WS server: {}", e);
            }
        });

        let scheme = if config.tls_cert_path.is_some() { "wss" } else { "ws" };
        info!("Audio streaming endpoint on {}://{}", scheme, config.listen_addr);
        Ok(Self {
            config, protocol, runtime: rt, sessions, event_tx: etx, event_rx: erx,
            cancel, recording_mgr,
        })
    }

    // ─── Audio send/recv ─────────────────────────────────────────────────

    pub fn send_audio_with_callback(&self, session_id: &str, frame: &AudioFrame, on_complete: CompletionCallback) -> Result<()> {
        let audio_buf = {
            let s = self.sessions.lock().unwrap();
            let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
            sess.audio_buf.clone()
        };
        audio_buf.push(&frame.data, on_complete)
            .map_err(|e| EndpointError::Other(e.into()))
    }

    pub fn send_audio(&self, session_id: &str, frame: &AudioFrame) -> Result<()> {
        self.send_audio_with_callback(session_id, frame, Box::new(|| {}))
    }

    pub fn send_background_audio(&self, session_id: &str, frame: &AudioFrame) -> Result<()> {
        let bg_buf = {
            let s = self.sessions.lock().unwrap();
            let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
            sess.bg_audio_buf.clone()
        };
        bg_buf.push_no_backpressure(&frame.data);
        Ok(())
    }

    pub fn recv_audio(&self, session_id: &str) -> Result<Option<AudioFrame>> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        Ok(sess.incoming_rx.try_recv().ok())
    }

    pub fn recv_audio_blocking(&self, session_id: &str, timeout_ms: u64) -> Result<Option<AudioFrame>> {
        let rx = {
            let s = self.sessions.lock().unwrap();
            s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?.incoming_rx.clone()
        };
        Ok(rx.recv_timeout(std::time::Duration::from_millis(timeout_ms)).ok())
    }

    // ─── Playback control ────────────────────────────────────────────────

    pub fn mute(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        sess.muted.store(true, Ordering::Relaxed); Ok(())
    }

    pub fn unmute(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        sess.muted.store(false, Ordering::Relaxed); Ok(())
    }

    pub fn pause(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        sess.paused.store(true, Ordering::Relaxed);
        // Send clearAudio to immediately stop playback on Plivo's side.
        // Plivo doesn't support muteStream — clearAudio is the only way to stop playback.
        // The Rust AudioBuffer retains queued audio for resume (send loop skips drain while paused).
        let json = self.protocol.build_clear_audio(&sess.stream_id);
        let _ = sess.ws_tx.send(Message::Text(json));
        debug!("Paused session {} (clearAudio sent to provider)", session_id);
        Ok(())
    }

    pub fn resume(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        sess.paused.store(false, Ordering::Relaxed);
        // No unmute needed — send loop will resume sending audio from the Rust buffer.
        debug!("Resumed session {}", session_id);
        Ok(())
    }

    // ─── Buffer / checkpoint / flush ─────────────────────────────────────

    /// Clear buffered audio — drains local AudioBuffer AND sends clear command to provider.
    /// Any audio already in the WS send queue will be overridden by the provider's clear.
    pub fn clear_buffer(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        sess.audio_buf.clear();
        // Cancel any pending flush checkpoint — interrupt overrides playout
        *sess.pending_flush.lock().unwrap() = None;
        // Clear awaiting_checkpoint so late Plivo confirmations are ignored
        *sess.awaiting_checkpoint.lock().unwrap() = None;
        // Wake any blocked wait_for_playout so executor thread returns immediately
        {
            let (lock, cvar) = &*sess.checkpoint_notify;
            *lock.lock().unwrap() = Some("_cleared".into());
            cvar.notify_all();
        }
        // Only send clearAudio if not already paused (pause already sent clearAudio)
        if !sess.paused.load(Ordering::Relaxed) {
            let json = self.protocol.build_clear_audio(&sess.stream_id);
            sess.ws_tx.send(Message::Text(json)).map_err(|_| EndpointError::Other("WS send failed".into()))?;
        }
        Ok(())
    }

    pub fn checkpoint(&self, session_id: &str, name: Option<&str>) -> Result<String> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        let cp_name = name.map(String::from).unwrap_or_else(|| {
            format!("cp-{}", sess.checkpoint_counter.fetch_add(1, Ordering::Relaxed))
        });
        let json = self.protocol.build_checkpoint(&sess.stream_id, &cp_name);
        sess.ws_tx.send(Message::Text(json)).map_err(|_| EndpointError::Other("WS send failed".into()))?;
        debug!("Checkpoint '{}' sent for session {}", cp_name, session_id);
        Ok(cp_name)
    }

    /// Queue a flush checkpoint — the send loop sends it after all buffered audio.
    /// This ensures the checkpoint is ordered AFTER all playAudio messages,
    /// so Plivo's playedStream confirms when ALL audio has actually been played.
    pub fn flush(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        let cp_name = format!("cp-{}", sess.checkpoint_counter.fetch_add(1, Ordering::Relaxed));
        *sess.pending_flush.lock().unwrap() = Some(cp_name.clone());
        debug!("Flush: checkpoint '{}' queued for session {} (send loop will send after drain)", cp_name, session_id);
        Ok(())
    }

    pub fn wait_for_playout(&self, session_id: &str, timeout_ms: u64) -> Result<bool> {
        let notify = {
            let s = self.sessions.lock().unwrap();
            let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
            sess.checkpoint_notify.clone()
        };
        let (lock, cvar) = &*notify;
        let mut guard = lock.lock().unwrap();
        let (ref mut guard, timeout) = cvar.wait_timeout_while(guard, std::time::Duration::from_millis(timeout_ms), |cp| cp.is_none()).unwrap();
        **guard = None;
        Ok(!timeout.timed_out())
    }

    // ─── DTMF ────────────────────────────────────────────────────────────

    pub fn send_dtmf(&self, session_id: &str, digits: &str) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        let json = self.protocol.build_send_dtmf(digits);
        sess.ws_tx.send(Message::Text(json)).map_err(|_| EndpointError::Other("WS send failed".into()))?;
        info!("DTMF '{}' sent on session {}", digits, session_id);
        Ok(())
    }

    // ─── Recording ───────────────────────────────────────────────────────

    pub fn start_recording(&self, session_id: &str, path: &str, stereo: bool) -> Result<()> {
        let mode = if stereo { crate::recorder::RecordingMode::Stereo } else { crate::recorder::RecordingMode::Mono };
        let sample_rate = self.config.output_sample_rate;
        let rec = self.recording_mgr.start(session_id, path, mode, sample_rate);
        let s = self.sessions.lock().unwrap();
        if let Some(sess) = s.get(session_id) {
            *sess.recorder.lock().unwrap() = Some(rec);
        }
        Ok(())
    }

    pub fn stop_recording(&self, session_id: &str) -> Result<()> {
        self.recording_mgr.stop(session_id);
        if let Some(sess) = self.sessions.lock().unwrap().get(session_id) {
            *sess.recorder.lock().unwrap() = None;
        }
        Ok(())
    }

    // ─── Beep detection ──────────────────────────────────────────────────

    pub fn detect_beep(&self, session_id: &str, config: BeepDetectorConfig) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        *sess.beep_detector.lock().unwrap() = Some(BeepDetector::new(config));
        Ok(())
    }

    pub fn cancel_beep_detection(&self, session_id: &str) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        *sess.beep_detector.lock().unwrap() = None;
        Ok(())
    }

    // ─── Call control ────────────────────────────────────────────────────

    pub fn hangup(&self, session_id: &str) -> Result<()> {
        let call_id = {
            let sess = self.sessions.lock().unwrap().remove(session_id);
            match sess { Some(s) => { cleanup_session(session_id, &s, &self.recording_mgr); s.call_id.clone() }, None => return Ok(()) }
        };
        self.protocol.hangup(&call_id, &self.runtime);
        Ok(())
    }

    pub fn send_raw_message(&self, session_id: &str, message: &str) -> Result<()> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        sess.ws_tx.send(Message::Text(message.to_string())).map_err(|_| EndpointError::Other("WS send failed".into()))
    }

    // ─── Accessors ───────────────────────────────────────────────────────

    pub fn incoming_rx(&self, session_id: &str) -> Result<Receiver<AudioFrame>> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        Ok(sess.incoming_rx.clone())
    }

    pub fn checkpoint_notify(&self, session_id: &str) -> Result<Arc<(Mutex<Option<String>>, Condvar)>> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        Ok(sess.checkpoint_notify.clone())
    }

    pub fn queued_frames(&self, session_id: &str) -> Result<usize> {
        let spf = (self.config.output_sample_rate * 20 / 1000) as usize;
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        Ok(sess.audio_buf.len() / spf)
    }

    pub fn queued_duration_ms(&self, session_id: &str) -> Result<f64> {
        let s = self.sessions.lock().unwrap();
        let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
        Ok(sess.audio_buf.queued_duration_ms(self.config.output_sample_rate))
    }

    pub fn wait_for_playout_notify(&self, session_id: &str, on_complete: crate::sip::audio_buffer::CompletionCallback) -> Result<()> {
        let audio_buf = {
            let s = self.sessions.lock().unwrap();
            let sess = s.get(session_id).ok_or_else(|| EndpointError::CallNotActive(session_id.to_string()))?;
            sess.audio_buf.clone()
        };
        audio_buf.set_playout_callback(on_complete);
        Ok(())
    }

    pub fn input_sample_rate(&self) -> u32 { self.config.input_sample_rate }
    pub fn output_sample_rate(&self) -> u32 { self.config.output_sample_rate }
    pub fn events(&self) -> Receiver<EndpointEvent> { self.event_rx.clone() }

    pub fn shutdown(&self) -> Result<()> {
        if self.cancel.is_cancelled() { return Ok(()); }
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

// ─── TLS helpers ──────────────────────────────────────────────────────────────

fn load_tls_acceptor(cert_path: &str, key_path: &str) -> std::result::Result<tokio_rustls::TlsAcceptor, Box<dyn std::error::Error>> {
    use std::io::BufReader;
    use tokio_rustls::rustls::{self, crypto::aws_lc_rs};

    // Install crypto provider if not yet set (needed when both aws-lc-rs and ring are in deps)
    let _ = aws_lc_rs::default_provider().install_default();

    let cert_file = std::fs::File::open(cert_path)?;
    let certs: Vec<_> = rustls_pemfile::certs(&mut BufReader::new(cert_file))
        .collect::<std::result::Result<Vec<_>, _>>()?;
    if certs.is_empty() {
        return Err("No certificates found in cert file".into());
    }

    let key_file = std::fs::File::open(key_path)?;
    let key = rustls_pemfile::private_key(&mut BufReader::new(key_file))?
        .ok_or("No private key found in key file")?;

    let config = rustls::ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(certs, key)?;

    Ok(tokio_rustls::TlsAcceptor::from(Arc::new(config)))
}

// ─── WebSocket server ────────────────────────────────────────────────────────

async fn run_ws_server(
    addr: &str,
    sessions: Arc<Mutex<HashMap<String, StreamSession>>>,
    etx: Sender<EndpointEvent>,
    cancel: CancellationToken,
    input_sample_rate: u32, output_sample_rate: u32,
    protocol: Arc<dyn StreamProtocol>,
    tls_acceptor: Option<tokio_rustls::TlsAcceptor>,
    recording_mgr: Arc<crate::recorder::RecordingManager>,
) -> std::result::Result<(), Box<dyn std::error::Error>> {
    let listener = TcpListener::bind(addr).await?;
    loop {
        tokio::select! {
            _ = cancel.cancelled() => break,
            result = listener.accept() => {
                let (stream, peer) = match result {
                    Ok(v) => v,
                    Err(e) => { warn!("TCP accept error: {}", e); continue; }
                };
                info!("WS connection from {}", peer);
                let sid = format!("ws-{:016x}", rand::random::<u64>());
                let (s, e, c, p, r) = (sessions.clone(), etx.clone(), cancel.clone(), protocol.clone(), recording_mgr.clone());

                if let Some(ref acceptor) = tls_acceptor {
                    let acceptor = acceptor.clone();
                    tokio::spawn(async move {
                        let tls_stream = match acceptor.accept(stream).await {
                            Ok(s) => s,
                            Err(e) => { warn!("TLS handshake failed from {}: {}", peer, e); return; }
                        };
                        debug!("TLS handshake complete from {}", peer);
                        let ws = match tokio_tungstenite::accept_async(tls_stream).await {
                            Ok(ws) => ws,
                            Err(e) => { warn!("WS handshake failed from {}: {}", peer, e); return; }
                        };
                        handle_ws(ws, sid, s, e, c, input_sample_rate, output_sample_rate, p, r).await;
                    });
                } else {
                    tokio::spawn(async move {
                        let ws = match tokio_tungstenite::accept_async(stream).await {
                            Ok(ws) => ws,
                            Err(e) => { warn!("WS handshake failed from {}: {}", peer, e); return; }
                        };
                        handle_ws(ws, sid, s, e, c, input_sample_rate, output_sample_rate, p, r).await;
                    });
                }
            }
        }
    }
    Ok(())
}

// ─── Per-connection WebSocket handler ────────────────────────────────────────

async fn handle_ws<S: AsyncRead + AsyncWrite + Unpin + Send + 'static>(
    ws: WebSocketStream<S>,
    sid: String,
    sessions: Arc<Mutex<HashMap<String, StreamSession>>>,
    etx: Sender<EndpointEvent>,
    cancel: CancellationToken,
    input_sample_rate: u32, output_sample_rate: u32,
    protocol: Arc<dyn StreamProtocol>,
    recording_mgr: Arc<crate::recorder::RecordingManager>,
) {
    use futures_util::{SinkExt, StreamExt};
    let (mut sink, mut stream) = ws.split();
    let (ws_tx, mut ws_rx) = tokio::sync::mpsc::unbounded_channel::<Message>();
    let (itx, irx) = crossbeam_channel::unbounded();

    let cc = cancel.clone();
    tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = cc.cancelled() => break,
                msg = ws_rx.recv() => {
                    match msg { Some(m) => { if sink.send(m).await.is_err() { break; } }, None => break }
                }
            }
        }
    });

    let mut encoding = WireEncoding::MulawRate8k;
    let mut upsampler: Option<crate::sip::resampler::Resampler> = None;
    let mut media_recorder: Option<Arc<Mutex<Option<Arc<crate::recorder::CallRecorder>>>>> = None;
    let mut media_beep_det: Option<Arc<Mutex<Option<BeepDetector>>>> = None;
    let mut media_active_sent = false;

    loop {
        tokio::select! {
            _ = cancel.cancelled() => break,
            msg = stream.next() => {
                let msg = match msg {
                    Some(Ok(Message::Text(t))) => t,
                    Some(Ok(Message::Close(_))) | None => break,
                    _ => continue,
                };

                let event = match protocol.parse_message(&msg) {
                    Some(e) => e,
                    None => continue,
                };

                match event {
                    StreamEvent::Start { call_id, stream_id, encoding: enc, headers } => {
                        encoding = enc;
                        upsampler = None; // Reset for new encoding

                        let audio_buf = Arc::new(AudioBuffer::with_queue_size(200, output_sample_rate));
                        let bg_audio_buf = Arc::new(AudioBuffer::with_queue_size(200, output_sample_rate));
                        let muted = Arc::new(AtomicBool::new(false));
                        let paused = Arc::new(AtomicBool::new(false));
                        let playout_notify = Arc::new((Mutex::new(false), Condvar::new()));
                        let cp_notify = Arc::new((Mutex::new(None), Condvar::new()));
                        let send_loop_notify = Arc::new(tokio::sync::Notify::new());
                        let session_recorder: Arc<Mutex<Option<Arc<crate::recorder::CallRecorder>>>> = Arc::new(Mutex::new(None));
                        let session_beep_detector: Arc<Mutex<Option<BeepDetector>>> = Arc::new(Mutex::new(None));
                        let pending_flush: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
                        let awaiting_checkpoint: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
                        let session_cancel = CancellationToken::new();

                        // Spawn send loop
                        let wstx = ws_tx.clone();
                        let ab = audio_buf.clone();
                        let bg = bg_audio_buf.clone();
                        let rec_send = session_recorder.clone();
                        let (m, p, pn) = (muted.clone(), paused.clone(), playout_notify.clone());
                        let pf = pending_flush.clone();
                        let aw = awaiting_checkpoint.clone();
                        let sc = session_cancel.clone();
                        let stream_id_for_loop = stream_id.clone();
                        // Send loop: 20ms interval pacing (matches SIP RTP send loop).
                        // Drains 20ms of audio from agent + background buffers, mixes, encodes, sends.
                        // No checkpoint blocking — audio flows at real-time pace.
                        let chunk_spf: usize = (output_sample_rate * 20 / 1000) as usize; // 20ms chunks
                        let send_proto = protocol.clone();
                        let send_enc = encoding;

                        tokio::spawn(async move {
                            let mut resampler = crate::sip::resampler::Resampler::new_voip(output_sample_rate, send_enc.sample_rate());
                            let mut interval = tokio::time::interval(Duration::from_millis(20));
                            interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

                            loop {
                                tokio::select! {
                                    _ = sc.cancelled() => break,
                                    _ = interval.tick() => {}
                                }

                                // Drain background audio regardless of pause state
                                let bg_samples = bg.drain(chunk_spf);

                                if p.load(Ordering::Relaxed) {
                                    // Paused: send background audio only (no agent voice)
                                    if !bg_samples.is_empty() {
                                        let encoded = send_enc.encode(&bg_samples, &mut resampler);
                                        let play_msg = send_proto.build_play_audio(&encoded, send_enc, &stream_id_for_loop);
                                        let _ = wstx.send(Message::Text(play_msg));
                                    }
                                    // If agent buffer already drained before pause, send pending checkpoint
                                    if ab.is_empty() {
                                        if let Some(cp_name) = pf.lock().unwrap().take() {
                                            *aw.lock().unwrap() = Some(cp_name.clone());
                                            let cp_msg = send_proto.build_checkpoint(&stream_id_for_loop, &cp_name);
                                            let _ = wstx.send(Message::Text(cp_msg));
                                            debug!("Send loop: flush checkpoint '{}' sent (paused, buffer empty)", cp_name);
                                        }
                                    }
                                    continue;
                                }

                                let voice = ab.drain(chunk_spf);

                                let has_voice = !voice.is_empty();
                                let has_bg = !bg_samples.is_empty();

                                // Record agent audio — always write to keep in sync with user channel
                                if let Ok(guard) = rec_send.lock() {
                                    if let Some(ref rec) = *guard {
                                        if has_voice || has_bg {
                                            let mixed_for_rec = if has_voice && has_bg {
                                                let len = voice.len().max(bg_samples.len());
                                                let mut out = Vec::with_capacity(len);
                                                for i in 0..len {
                                                    let v = if i < voice.len() { voice[i] as i32 } else { 0 };
                                                    let b = if i < bg_samples.len() { bg_samples[i] as i32 } else { 0 };
                                                    out.push((v + b).clamp(-32768, 32767) as i16);
                                                }
                                                out
                                            } else if has_voice { voice.clone() } else { bg_samples.clone() };
                                            rec.write_agent_samples(&mixed_for_rec);
                                        } else {
                                            rec.write_agent_samples(&vec![0i16; chunk_spf]);
                                        }
                                    }
                                }

                                if has_voice || has_bg {
                                    let mixed = if has_voice && has_bg {
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
                                        let encoded = send_enc.encode(&mixed, &mut resampler);
                                        let play_msg = send_proto.build_play_audio(&encoded, send_enc, &stream_id_for_loop);
                                        let _ = wstx.send(Message::Text(play_msg));
                                    }
                                } else {
                                    // Buffer empty — send queued flush checkpoint if any.
                                    // This guarantees checkpoint is AFTER all playAudio messages,
                                    // so Plivo's playedStream confirms actual playout completion.
                                    if let Some(cp_name) = pf.lock().unwrap().take() {
                                        *aw.lock().unwrap() = Some(cp_name.clone());
                                        let cp_msg = send_proto.build_checkpoint(&stream_id_for_loop, &cp_name);
                                        let _ = wstx.send(Message::Text(cp_msg));
                                        debug!("Send loop: flush checkpoint '{}' sent (buffer drained)", cp_name);
                                    }
                                    if ab.is_empty() {
                                        if let Ok(mut d) = pn.0.lock() { *d = true; pn.1.notify_all(); }
                                    }
                                }
                            }
                        });

                        media_recorder = Some(session_recorder.clone());
                        media_beep_det = Some(session_beep_detector.clone());

                        sessions.lock().unwrap().insert(sid.clone(), StreamSession {
                            call_id: call_id.clone(), stream_id: stream_id.clone(),
                            ws_tx: ws_tx.clone(), incoming_tx: itx.clone(), incoming_rx: irx.clone(),
                            audio_buf, bg_audio_buf, extra_headers: headers.clone(), encoding,
                            muted, paused, playout_notify,
                            checkpoint_counter: AtomicU64::new(0), checkpoint_notify: cp_notify,
                            send_loop_notify, pending_flush, awaiting_checkpoint,
                            recorder: session_recorder,
                            beep_detector: session_beep_detector,
                            cancel: session_cancel,
                        });

                        let mut session = crate::sip::call::CallSession::new(sid.clone(), crate::sip::call::CallDirection::Inbound);
                        session.call_uuid = Some(call_id.clone());
                        session.remote_uri = call_id;
                        session.local_uri = stream_id;
                        session.extra_headers = headers;
                        let _ = etx.try_send(EndpointEvent::IncomingCall { session });
                        // CallMediaActive fired on first audio frame (matches SIP pattern
                        // where media_active fires when RTP starts, not on INVITE).
                        // This gives the agent pipeline time to initialize before audio flows.
                        info!("Session {} started (encoding={:?})", sid, encoding);
                    }

                    StreamEvent::Media { payload } => {
                        // Fire CallMediaActive on first audio frame (matches SIP pattern)
                        if !media_active_sent {
                            media_active_sent = true;
                            let _ = etx.try_send(EndpointEvent::CallMediaActive { call_id: sid.clone() });
                            debug!("First media frame → CallMediaActive on session {}", sid);
                        }
                        let pcm = {
                            let native = encoding.decode(&payload);
                            let wire_rate = encoding.sample_rate();
                            if upsampler.is_none() {
                                upsampler = crate::sip::resampler::Resampler::new_voip(wire_rate, input_sample_rate);
                            }
                            if let Some(ref mut us) = upsampler {
                                us.process(&native).to_vec()
                            } else {
                                native
                            }
                        };

                        if let Some(ref rec_ref) = media_recorder {
                            if let Ok(guard) = rec_ref.lock() {
                                if let Some(ref rec) = *guard { rec.write_user_samples(&pcm); }
                            }
                        }

                        if let Some(ref bd_ref) = media_beep_det {
                            if let Ok(mut g) = bd_ref.lock() {
                                if let Some(ref mut det) = *g {
                                    match det.process_frame(&pcm) {
                                        BeepDetectorResult::Detected(e) => {
                                            let _ = etx.try_send(EndpointEvent::BeepDetected { call_id: sid.clone(), frequency_hz: e.frequency_hz, duration_ms: e.duration_ms });
                                            *g = None;
                                        }
                                        BeepDetectorResult::Timeout => {
                                            let _ = etx.try_send(EndpointEvent::BeepTimeout { call_id: sid.clone() });
                                            *g = None;
                                        }
                                        _ => {}
                                    }
                                }
                            }
                        }

                        let n = pcm.len() as u32;
                        let _ = itx.try_send(AudioFrame { data: pcm, sample_rate: input_sample_rate, num_channels: 1, samples_per_channel: n });
                    }

                    StreamEvent::Dtmf { digit } => {
                        let _ = etx.try_send(EndpointEvent::DtmfReceived { call_id: sid.clone(), digit, method: "audio_stream".into() });
                    }

                    StreamEvent::CheckpointAck { name } => {
                        debug!("Checkpoint '{}' confirmed on session {}", name, sid);
                        if let Some(sess) = sessions.lock().unwrap().get(sid.as_str()) {
                            // Only accept if this matches the checkpoint we're waiting for.
                            // Prevents stale confirmations (from cancelled flushes) from
                            // polluting the condvar for the next flush cycle.
                            let matches = sess.awaiting_checkpoint.lock().unwrap()
                                .as_ref()
                                .map(|expected| *expected == name)
                                .unwrap_or(false);
                            if matches {
                                *sess.awaiting_checkpoint.lock().unwrap() = None;
                                let (lock, cvar) = &*sess.checkpoint_notify;
                                *lock.lock().unwrap() = Some(name);
                                cvar.notify_all();
                            } else {
                                debug!("Ignoring stale checkpoint '{}' on session {} (not awaiting)", name, sid);
                            }
                        }
                    }

                    StreamEvent::BufferCleared => {
                        debug!("Buffer cleared confirmed on session {}", sid);
                    }

                    StreamEvent::PlayFailed { reason } => {
                        warn!("Playback failed on session {}: {}", sid, reason);
                        // Clear stale audio to prevent accumulation after failure
                        if let Some(sess) = sessions.lock().unwrap().get(&sid) {
                            sess.audio_buf.clear();
                        }
                    }

                    StreamEvent::StreamError { reason } => {
                        warn!("Stream error on session {}: {}", sid, reason);
                        if let Some(sess) = sessions.lock().unwrap().remove(&sid) {
                            cleanup_session(&sid, &sess, &recording_mgr);
                            let session = crate::sip::call::CallSession::new(sid.clone(), crate::sip::call::CallDirection::Inbound);
                            let _ = etx.try_send(EndpointEvent::CallTerminated { session, reason: format!("stream error: {}", reason) });
                        }
                        break;
                    }

                    StreamEvent::MuteStream => {
                        info!("Session {} muted by provider", sid);
                        if let Some(sess) = sessions.lock().unwrap().get(&sid) {
                            sess.muted.store(true, std::sync::atomic::Ordering::Relaxed);
                        }
                    }

                    StreamEvent::UnmuteStream => {
                        info!("Session {} unmuted by provider", sid);
                        if let Some(sess) = sessions.lock().unwrap().get(&sid) {
                            sess.muted.store(false, std::sync::atomic::Ordering::Relaxed);
                        }
                    }

                    StreamEvent::Stop => {
                        info!("Session {} stopped", sid);
                        if let Some(sess) = sessions.lock().unwrap().remove(&sid) {
                            cleanup_session(&sid, &sess, &recording_mgr);
                            let session = crate::sip::call::CallSession::new(sid.clone(), crate::sip::call::CallDirection::Inbound);
                            let _ = etx.try_send(EndpointEvent::CallTerminated { session, reason: "stream stopped".into() });
                        }
                        break;
                    }
                }
            }
        }
    }

    // Cleanup on WS disconnect
    if let Some(sess) = sessions.lock().unwrap().remove(&sid) {
        cleanup_session(&sid, &sess, &recording_mgr);
        let session = crate::sip::call::CallSession::new(sid.clone(), crate::sip::call::CallDirection::Inbound);
        let _ = etx.try_send(EndpointEvent::CallTerminated { session, reason: "ws disconnected".into() });
        info!("Session {} cleaned up (WS disconnected)", sid);
    }
}

/// Clean up a session — cancel send loop and wake any blocked wait_for_playout.
fn cleanup_session(session_id: &str, sess: &StreamSession, recording_mgr: &Arc<crate::recorder::RecordingManager>) {
    sess.cancel.cancel();
    // Stop recording — wakes encoder thread for immediate finalization
    recording_mgr.stop(session_id);
    // Wake blocked wait_for_playout so executor threads don't hang for 30s
    let (lock, cvar) = &*sess.checkpoint_notify;
    *lock.lock().unwrap() = Some("_closed".into());
    cvar.notify_all();
}
