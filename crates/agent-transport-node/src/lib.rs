use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use napi::bindgen_prelude::*;
use napi::threadsafe_function::{
    ErrorStrategy, ThreadSafeCallContext, ThreadsafeFunction, ThreadsafeFunctionCallMode,
};
use napi::JsFunction;
use napi_derive::napi;

use agent_transport_core::{
    AudioFrame as RustAudioFrame, BeepDetectorConfig as RustBeepConfig,
    CallSession as RustCallSession, Codec as RustCodec,
    EndpointConfig as RustEndpointConfig, EndpointEvent, SipEndpoint as RustSipEndpoint,
};
use agent_transport_core::audio_stream::config::AudioStreamConfig as RustAudioStreamConfig;
use agent_transport_core::audio_stream::endpoint::AudioStreamEndpoint as RustAudioStreamEndpoint;

fn napi_err(e: impl std::fmt::Display) -> napi::Error {
    Error::from_reason(e.to_string())
}

// ─── Async tasks for blocking operations ─────────────────────────────────────

use napi::Task;

pub struct RecvAudioTask {
    rx: crossbeam_channel::Receiver<agent_transport_core::AudioFrame>,
    timeout_ms: u64,
}

impl Task for RecvAudioTask {
    type Output = Option<Vec<u8>>;
    type JsValue = Option<Vec<u8>>;
    fn compute(&mut self) -> Result<Self::Output> {
        Ok(self.rx.recv_timeout(std::time::Duration::from_millis(self.timeout_ms)).ok().map(|f| f.as_bytes()))
    }
    fn resolve(&mut self, _env: Env, output: Self::Output) -> Result<Self::JsValue> {
        Ok(output)
    }
}

pub struct WaitForPlayoutTask {
    notify: Arc<(std::sync::Mutex<Option<String>>, std::sync::Condvar)>,
    timeout_ms: u64,
}

impl Task for WaitForPlayoutTask {
    type Output = bool;
    type JsValue = bool;
    fn compute(&mut self) -> Result<Self::Output> {
        let (lock, cvar) = &*self.notify;
        let guard = lock.lock().unwrap();
        let result = cvar.wait_timeout_while(guard, std::time::Duration::from_millis(self.timeout_ms), |cp| cp.is_none()).unwrap();
        Ok(!result.1.timed_out())
    }
    fn resolve(&mut self, _env: Env, output: Self::Output) -> Result<Self::JsValue> {
        Ok(output)
    }
}

pub struct SipWaitForPlayoutTask {
    notify: Arc<(std::sync::Mutex<bool>, std::sync::Condvar)>,
    timeout_ms: u64,
}

impl Task for SipWaitForPlayoutTask {
    type Output = bool;
    type JsValue = bool;
    fn compute(&mut self) -> Result<Self::Output> {
        let (lock, cvar) = &*self.notify;
        let guard = lock.lock().unwrap();
        let result = cvar.wait_timeout_while(guard, std::time::Duration::from_millis(self.timeout_ms), |done| !*done).unwrap();
        Ok(!result.1.timed_out())
    }
    fn resolve(&mut self, _env: Env, output: Self::Output) -> Result<Self::JsValue> {
        Ok(output)
    }
}

/// AudioFrame compatible with LiveKit's format.
#[napi(object)]
pub struct AudioFrame {
    pub data: Vec<i32>, // napi doesn't support i16 directly; use i32
    pub sample_rate: u32,
    pub num_channels: u32,
    pub samples_per_channel: u32,
}

impl AudioFrame {
    fn from_rust(f: RustAudioFrame) -> Self {
        Self {
            data: f.data.iter().map(|&s| s as i32).collect(),
            sample_rate: f.sample_rate,
            num_channels: f.num_channels,
            samples_per_channel: f.samples_per_channel,
        }
    }

    fn to_rust(&self) -> RustAudioFrame {
        let data: Vec<i16> = self.data.iter().map(|&s| s as i16).collect();
        RustAudioFrame::new(data, self.sample_rate, self.num_channels)
    }
}

/// CallSession info.
#[napi(object)]
#[derive(Clone)]
pub struct CallSession {
    pub call_id: String,
    pub call_uuid: Option<String>,
    pub direction: String,
    pub state: String,
    pub remote_uri: String,
    pub local_uri: String,
    pub extra_headers: HashMap<String, String>,
}

impl From<RustCallSession> for CallSession {
    fn from(s: RustCallSession) -> Self {
        Self {
            call_id: s.call_id,
            call_uuid: s.call_uuid,
            direction: format!("{:?}", s.direction),
            state: format!("{:?}", s.state),
            remote_uri: s.remote_uri,
            local_uri: s.local_uri,
            extra_headers: s.extra_headers,
        }
    }
}

/// Endpoint configuration.
#[napi(object)]
pub struct EndpointConfig {
    pub sip_server: Option<String>,
    pub stun_server: Option<String>,
    pub codecs: Option<Vec<String>>,
    pub log_level: Option<u32>,
    /// Enable adaptive jitter buffer (requires jitter-buffer feature).
    pub jitter_buffer: Option<bool>,
    /// Enable packet loss concealment (requires plc feature).
    pub plc: Option<bool>,
    /// Enable comfort noise generation (requires comfort-noise feature).
    pub comfort_noise: Option<bool>,
}

/// Structured event info returned by pollEvent() and waitForEvent().
#[napi(object)]
#[derive(Clone)]
pub struct EventInfo {
    pub event_type: String,
    pub call_id: Option<String>,
    pub session: Option<CallSession>,
    pub error: Option<String>,
    pub reason: Option<String>,
    pub digit: Option<String>,
    pub method: Option<String>,
    pub frequency_hz: Option<f64>,
    pub duration_ms: Option<u32>,
}

fn event_to_info(event: &EndpointEvent) -> EventInfo {
    match event {
        EndpointEvent::Registered => EventInfo {
            event_type: "registered".into(),
            call_id: None,
            session: None,
            error: None,
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
        },
        EndpointEvent::RegistrationFailed { error } => EventInfo {
            event_type: "registration_failed".into(),
            call_id: None,
            session: None,
            error: Some(error.clone()),
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
        },
        EndpointEvent::Unregistered => EventInfo {
            event_type: "unregistered".into(),
            call_id: None,
            session: None,
            error: None,
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
        },
        EndpointEvent::IncomingCall { session } => EventInfo {
            event_type: "incoming_call".into(),
            call_id: Some(session.call_id.clone()),
            session: Some(session.clone().into()),
            error: None,
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
        },
        EndpointEvent::CallStateChanged { session } => EventInfo {
            event_type: "call_state".into(),
            call_id: Some(session.call_id.clone()),
            session: Some(session.clone().into()),
            error: None,
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
        },
        EndpointEvent::CallMediaActive { call_id } => EventInfo {
            event_type: "call_media_active".into(),
            call_id: Some(call_id.clone()),
            session: None,
            error: None,
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
        },
        EndpointEvent::CallTerminated { session, reason } => EventInfo {
            event_type: "call_terminated".into(),
            call_id: Some(session.call_id.clone()),
            session: Some(session.clone().into()),
            error: None,
            reason: Some(reason.clone()),
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
        },
        EndpointEvent::DtmfReceived {
            call_id,
            digit,
            method,
        } => EventInfo {
            event_type: "dtmf_received".into(),
            call_id: Some(call_id.clone()),
            session: None,
            error: None,
            reason: None,
            digit: Some(digit.to_string()),
            method: Some(method.clone()),
            frequency_hz: None,
            duration_ms: None,
        },
        EndpointEvent::BeepDetected {
            call_id,
            frequency_hz,
            duration_ms,
        } => EventInfo {
            event_type: "beep_detected".into(),
            call_id: Some(call_id.clone()),
            session: None,
            error: None,
            reason: None,
            digit: None,
            method: None,
            frequency_hz: Some(*frequency_hz),
            duration_ms: Some(*duration_ms),
        },
        EndpointEvent::BeepTimeout { call_id } => EventInfo {
            event_type: "beep_timeout".into(),
            call_id: Some(call_id.clone()),
            session: None,
            error: None,
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
        },
    }
}

type EventTsfn = ThreadsafeFunction<EventInfo, ErrorStrategy::CalleeHandled>;

/// SIP endpoint — call control and audio I/O.
#[napi]
pub struct SipEndpoint {
    inner: RustSipEndpoint,
    callbacks: Arc<Mutex<HashMap<String, Vec<EventTsfn>>>>,
    event_thread_running: Arc<AtomicBool>,
}

#[napi]
impl SipEndpoint {
    #[napi(constructor)]
    pub fn new(config: Option<EndpointConfig>) -> Result<Self> {
        let cfg = config.unwrap_or(EndpointConfig {
            sip_server: None,
            stun_server: None,
            codecs: None,
            log_level: None,
            jitter_buffer: None,
            plc: None,
            comfort_noise: None,
        });

        let codec_list = cfg
            .codecs
            .unwrap_or_else(|| vec!["pcmu".into(), "pcma".into()])
            .iter()
            .filter_map(|c| match c.to_lowercase().as_str() {
                "pcmu" => Some(RustCodec::PCMU),
                "pcma" => Some(RustCodec::PCMA),
                _ => None,
            })
            .collect();

        let rust_config = RustEndpointConfig {
            sip_server: cfg.sip_server.unwrap_or_else(|| "phone.plivo.com".into()),
            stun_server: cfg.stun_server.unwrap_or_else(|| "stun-fb.plivo.com:3478".into()),
            codecs: codec_list,
            log_level: cfg.log_level.unwrap_or(3),
            audio_processing: agent_transport_core::AudioProcessingConfig {
                jitter_buffer: cfg.jitter_buffer.unwrap_or(false),
                plc: cfg.plc.unwrap_or(false),
                comfort_noise: cfg.comfort_noise.unwrap_or(false),
                ..Default::default()
            },
            ..Default::default()
        };

        let inner = RustSipEndpoint::new(rust_config)
            .map_err(napi_err)?;

        Ok(Self {
            inner,
            callbacks: Arc::new(Mutex::new(HashMap::new())),
            event_thread_running: Arc::new(AtomicBool::new(false)),
        })
    }

    /// Register an event listener. Events: registered, registration_failed,
    /// unregistered, incoming_call, call_state, call_media_active,
    /// call_terminated, dtmf_received, beep_detected, beep_timeout
    ///
    /// ```js
    /// ep.on('incoming_call', (event) => {
    ///   console.log(event.session.remoteUri);
    ///   ep.answer(event.callId);
    /// });
    /// ```
    #[napi(
        ts_args_type = "eventName: string, callback: (event: EventInfo) => void"
    )]
    pub fn on(&self, event_name: String, callback: JsFunction) -> Result<()> {
        let tsfn: EventTsfn =
            callback.create_threadsafe_function(0, |ctx: ThreadSafeCallContext<EventInfo>| {
                Ok(vec![ctx.value])
            })?;

        self.callbacks
            .lock()
            .unwrap()
            .entry(event_name)
            .or_default()
            .push(tsfn);

        self.ensure_event_loop();
        Ok(())
    }

    #[napi]
    pub fn register(&self, username: String, password: String) -> Result<()> {
        self.inner
            .register(&username, &password)
            .map_err(napi_err)
    }

    #[napi]
    pub fn unregister(&self) -> Result<()> {
        self.inner
            .unregister()
            .map_err(napi_err)
    }

    #[napi]
    pub fn is_registered(&self) -> bool {
        self.inner.is_registered()
    }

    /// Make an outbound call. Optional headers object adds custom SIP headers.
    #[napi]
    pub fn call(
        &self,
        dest_uri: String,
        headers: Option<HashMap<String, String>>,
    ) -> Result<String> {
        self.inner
            .call(&dest_uri, headers)
            .map_err(napi_err)
    }

    #[napi]
    pub fn answer(&self, call_id: String, code: Option<u32>) -> Result<()> {
        self.inner
            .answer(&call_id, code.unwrap_or(200) as u16)
            .map_err(napi_err)
    }

    #[napi]
    pub fn reject(&self, call_id: String, code: Option<u32>) -> Result<()> {
        self.inner
            .reject(&call_id, code.unwrap_or(486) as u16)
            .map_err(napi_err)
    }

    #[napi]
    pub fn hangup(&self, call_id: String) -> Result<()> {
        self.inner
            .hangup(&call_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn send_dtmf(&self, call_id: String, digits: String, method: Option<String>) -> Result<()> {
        self.inner
            .send_dtmf_with_method(&call_id, &digits, method.as_deref().unwrap_or("rfc2833"))
            .map_err(napi_err)
    }

    /// Send a SIP INFO message with custom content type and body.
    #[napi]
    pub fn send_info(&self, call_id: String, content_type: String, body: String) -> Result<()> {
        self.inner.send_info(&call_id, &content_type, &body).map_err(napi_err)
    }

    #[napi]
    pub fn transfer(&self, call_id: String, dest_uri: String) -> Result<()> {
        self.inner
            .transfer(&call_id, &dest_uri)
            .map_err(napi_err)
    }

    #[napi]
    pub fn transfer_attended(&self, call_id: String, target_call_id: String) -> Result<()> {
        self.inner
            .transfer_attended(&call_id, &target_call_id)
            .map_err(napi_err)
    }

    /// SIP hold — send Re-INVITE with a=sendonly
    #[napi]
    pub fn hold(&self, call_id: String) -> Result<()> { self.inner.hold(&call_id).map_err(napi_err) }

    /// SIP unhold — send Re-INVITE with a=sendrecv
    #[napi]
    pub fn unhold(&self, call_id: String) -> Result<()> { self.inner.unhold(&call_id).map_err(napi_err) }

    #[napi]
    pub fn mute(&self, call_id: String) -> Result<()> {
        self.inner
            .mute(&call_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn unmute(&self, call_id: String) -> Result<()> {
        self.inner
            .unmute(&call_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn send_audio(&self, call_id: String, frame: AudioFrame) -> Result<()> {
        self.inner
            .send_audio(&call_id, &frame.to_rust())
            .map_err(napi_err)
    }

    #[napi]
    pub fn recv_audio(&self, call_id: String) -> Result<Option<AudioFrame>> {
        self.inner
            .recv_audio(&call_id)
            .map(|opt| opt.map(AudioFrame::from_rust))
            .map_err(napi_err)
    }

    /// Receive audio as raw PCM bytes (little-endian int16). No JS array conversion.
    #[napi]
    pub fn recv_audio_bytes(&self, call_id: String) -> Result<Option<Vec<u8>>> {
        self.inner.recv_audio(&call_id).map(|opt| opt.map(|f| f.as_bytes())).map_err(napi_err)
    }

    /// Send raw PCM bytes (little-endian int16) directly.
    #[napi]
    pub fn send_audio_bytes(&self, call_id: String, audio: Vec<u8>, sample_rate: u32, num_channels: u32) -> Result<()> {
        let frame = RustAudioFrame::from_bytes(&audio, sample_rate, num_channels);
        self.inner.send_audio(&call_id, &frame).map_err(napi_err)
    }

    /// Send background audio to be mixed with agent voice in the RTP send loop.
    #[napi]
    pub fn send_background_audio(&self, call_id: String, audio: Vec<u8>, sample_rate: u32, num_channels: u32) -> Result<()> {
        let f = RustAudioFrame::from_bytes(&audio, sample_rate, num_channels);
        self.inner.send_background_audio(&call_id, &f).map_err(napi_err)
    }

    /// Send raw PCM bytes with async completion notification (backpressure).
    /// The callback fires when the buffer drains below threshold.
    /// Matches Python's send_audio_notify pattern.
    #[napi(ts_args_type = "callId: number, audio: Buffer, sampleRate: number, numChannels: number, notifyFn: () => void")]
    pub fn send_audio_notify(&self, call_id: String, audio: Vec<u8>, sample_rate: u32, num_channels: u32, notify_fn: JsFunction) -> Result<()> {
        let frame = RustAudioFrame::from_bytes(&audio, sample_rate, num_channels);
        let tsfn: ThreadsafeFunction<(), ErrorStrategy::CalleeHandled> =
            notify_fn.create_threadsafe_function(0, |ctx: ThreadSafeCallContext<()>| {
                Ok(vec![ctx.env.get_undefined()?])
            })?;
        let callback: Box<dyn FnOnce() + Send> = Box::new(move || {
            tsfn.call(Ok(()), ThreadsafeFunctionCallMode::NonBlocking);
        });
        self.inner.send_audio_with_callback(&call_id, &frame, callback).map_err(napi_err)
    }

    /// Receive audio frame, blocking until available or timeout (ms).
    #[napi]
    pub fn recv_audio_blocking(&self, call_id: String, timeout_ms: Option<u32>) -> Result<Option<AudioFrame>> {
        self.inner
            .recv_audio_blocking(&call_id, timeout_ms.unwrap_or(20) as u64)
            .map(|opt| opt.map(AudioFrame::from_rust))
            .map_err(napi_err)
    }

    /// Receive audio as raw bytes, blocking. Fastest path.
    #[napi]
    pub fn recv_audio_bytes_blocking(&self, call_id: String, timeout_ms: Option<u32>) -> Result<Option<Vec<u8>>> {
        self.inner
            .recv_audio_blocking(&call_id, timeout_ms.unwrap_or(20) as u64)
            .map(|opt| opt.map(|f| f.as_bytes()))
            .map_err(napi_err)
    }

    /// Receive audio as raw bytes, non-blocking Promise. Runs on libuv thread pool.
    #[napi(ts_return_type = "Promise<Buffer | null>")]
    pub fn recv_audio_bytes_async(&self, call_id: String, timeout_ms: Option<u32>) -> Result<AsyncTask<RecvAudioTask>> {
        let rx = self.inner.incoming_rx(&call_id).map_err(napi_err)?;
        Ok(AsyncTask::new(RecvAudioTask { rx, timeout_ms: timeout_ms.unwrap_or(20) as u64 }))
    }

    /// Wait for playout, non-blocking Promise. Runs on libuv thread pool.
    #[napi(ts_return_type = "Promise<boolean>")]
    pub fn wait_for_playout_async(&self, call_id: String, timeout_ms: Option<u32>) -> Result<AsyncTask<SipWaitForPlayoutTask>> {
        let notify = self.inner.playout_notify(&call_id).map_err(napi_err)?;
        Ok(AsyncTask::new(SipWaitForPlayoutTask { notify, timeout_ms: timeout_ms.unwrap_or(5000) as u64 }))
    }

    /// Number of audio frames queued for sending. Multiply by 0.02 for seconds.
    #[napi]
    pub fn queued_frames(&self, call_id: String) -> Result<u32> {
        self.inner.queued_frames(&call_id).map(|n| n as u32).map_err(napi_err)
    }

    /// Audio sample rate in Hz (always 16000).
    #[napi(getter)]
    pub fn sample_rate(&self) -> u32 {
        16000
    }

    /// Number of audio channels (always 1 = mono).
    #[napi(getter)]
    pub fn num_channels(&self) -> u32 {
        1
    }

    /// Start recording a call to a stereo WAV file (L=user, R=agent).
    /// Set stereo=false for mono (mixed).
    #[napi]
    pub fn start_recording(&self, call_id: String, path: String, stereo: Option<bool>) -> Result<()> {
        self.inner
            .start_recording(&call_id, &path, stereo.unwrap_or(true))
            .map_err(napi_err)
    }

    #[napi]
    pub fn stop_recording(&self, call_id: String) -> Result<()> {
        self.inner
            .stop_recording(&call_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn detect_beep(
        &self,
        call_id: String,
        timeout_ms: Option<u32>,
        min_duration_ms: Option<u32>,
        max_duration_ms: Option<u32>,
    ) -> Result<()> {
        let config = RustBeepConfig {
            sample_rate: 16000,
            timeout_ms: timeout_ms.unwrap_or(30000),
            min_duration_ms: min_duration_ms.unwrap_or(80),
            max_duration_ms: max_duration_ms.unwrap_or(5000),
            ..Default::default()
        };
        self.inner
            .detect_beep(&call_id, config)
            .map_err(napi_err)
    }

    #[napi]
    pub fn cancel_beep_detection(&self, call_id: String) -> Result<()> {
        self.inner
            .cancel_beep_detection(&call_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn flush(&self, call_id: String) -> Result<()> {
        self.inner
            .flush(&call_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn clear_buffer(&self, call_id: String) -> Result<()> {
        self.inner
            .clear_buffer(&call_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn wait_for_playout(&self, call_id: String, timeout_ms: Option<u32>) -> Result<bool> {
        self.inner
            .wait_for_playout(&call_id, timeout_ms.unwrap_or(5000) as u64)
            .map_err(napi_err)
    }

    #[napi]
    pub fn pause(&self, call_id: String) -> Result<()> {
        self.inner
            .pause(&call_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn resume(&self, call_id: String) -> Result<()> {
        self.inner
            .resume(&call_id)
            .map_err(napi_err)
    }

    /// Poll for the next event (non-blocking). Returns structured event or null.
    #[napi]
    pub fn poll_event(&self) -> Result<Option<EventInfo>> {
        match self.inner.events().try_recv() {
            Ok(event) => Ok(Some(event_to_info(&event))),
            Err(_) => Ok(None),
        }
    }

    /// Shut down the endpoint. Stops event dispatch and tears down SIP stack.
    #[napi]
    pub fn shutdown(&self) -> Result<()> {
        self.event_thread_running.store(false, Ordering::Relaxed);
        self.inner
            .shutdown()
            .map_err(napi_err)
    }
}

impl SipEndpoint {
    fn ensure_event_loop(&self) {
        if self.event_thread_running.swap(true, Ordering::Relaxed) {
            return;
        }

        let rx = self.inner.events();
        let callbacks = self.callbacks.clone();
        let running = self.event_thread_running.clone();

        std::thread::spawn(move || {
            while running.load(Ordering::Relaxed) {
                match rx.recv_timeout(Duration::from_millis(100)) {
                    Ok(event) => {
                        let name = event.callback_name();
                        let info = event_to_info(&event);

                        let cbs = callbacks.lock().unwrap();
                        if let Some(handlers) = cbs.get(name) {
                            for tsfn in handlers {
                                tsfn.call(
                                    Ok(info.clone()),
                                    ThreadsafeFunctionCallMode::NonBlocking,
                                );
                            }
                        }
                    }
                    Err(crossbeam_channel::RecvTimeoutError::Timeout) => continue,
                    Err(crossbeam_channel::RecvTimeoutError::Disconnected) => break,
                }
            }
        });
    }
}

// ─── AudioStreamEndpoint (Plivo WebSocket audio streaming) ───────────────────

#[napi(object)]
pub struct AudioStreamConfigJs {
    pub listen_addr: Option<String>,
    pub plivo_auth_id: Option<String>,
    pub plivo_auth_token: Option<String>,
    pub sample_rate: Option<u32>,
    pub auto_hangup: Option<bool>,
}

#[napi]
pub struct AudioStreamEndpoint {
    inner: RustAudioStreamEndpoint,
}

#[napi]
impl AudioStreamEndpoint {
    #[napi(constructor)]
    pub fn new(config: Option<AudioStreamConfigJs>) -> Result<Self> {
        let cfg = config.unwrap_or(AudioStreamConfigJs { listen_addr: None, plivo_auth_id: None, plivo_auth_token: None, sample_rate: None, auto_hangup: None });
        let rc = RustAudioStreamConfig {
            listen_addr: cfg.listen_addr.unwrap_or_else(|| "0.0.0.0:8080".into()),
            plivo_auth_id: cfg.plivo_auth_id.unwrap_or_default(),
            plivo_auth_token: cfg.plivo_auth_token.unwrap_or_default(),
            sample_rate: cfg.sample_rate.unwrap_or(16000),
            auto_hangup: cfg.auto_hangup.unwrap_or(true),
        };
        Ok(Self { inner: RustAudioStreamEndpoint::new(rc).map_err(napi_err)? })
    }

    #[napi]
    pub fn send_audio(&self, session_id: String, frame: AudioFrame) -> Result<()> {
        self.inner.send_audio(&session_id, &frame.to_rust()).map_err(napi_err)
    }

    #[napi]
    pub fn send_audio_bytes(&self, session_id: String, audio: Vec<u8>, sample_rate: u32, num_channels: u32) -> Result<()> {
        let f = RustAudioFrame::from_bytes(&audio, sample_rate, num_channels);
        self.inner.send_audio(&session_id, &f).map_err(napi_err)
    }

    /// Send background audio to be mixed with agent voice in the send loop.
    #[napi]
    pub fn send_background_audio(&self, session_id: String, audio: Vec<u8>, sample_rate: u32, num_channels: u32) -> Result<()> {
        let f = RustAudioFrame::from_bytes(&audio, sample_rate, num_channels);
        self.inner.send_background_audio(&session_id, &f).map_err(napi_err)
    }

    /// Send raw PCM bytes with async completion notification (backpressure).
    /// Matches SipEndpoint.send_audio_notify — used by SipAudioSource adapters.
    #[napi(ts_args_type = "sessionId: number, audio: Buffer, sampleRate: number, numChannels: number, notifyFn: () => void")]
    pub fn send_audio_notify(&self, session_id: String, audio: Vec<u8>, sample_rate: u32, num_channels: u32, notify_fn: JsFunction) -> Result<()> {
        let frame = RustAudioFrame::from_bytes(&audio, sample_rate, num_channels);
        let tsfn: ThreadsafeFunction<(), ErrorStrategy::CalleeHandled> =
            notify_fn.create_threadsafe_function(0, |ctx: ThreadSafeCallContext<()>| {
                Ok(vec![ctx.env.get_undefined()?])
            })?;
        let callback: Box<dyn FnOnce() + Send> = Box::new(move || {
            tsfn.call(Ok(()), ThreadsafeFunctionCallMode::NonBlocking);
        });
        self.inner.send_audio_with_callback(&session_id, &frame, callback).map_err(napi_err)
    }

    #[napi]
    pub fn recv_audio(&self, session_id: String) -> Result<Option<AudioFrame>> {
        self.inner.recv_audio(&session_id).map(|o| o.map(AudioFrame::from_rust)).map_err(napi_err)
    }

    #[napi]
    pub fn recv_audio_bytes(&self, session_id: String) -> Result<Option<Vec<u8>>> {
        self.inner.recv_audio(&session_id).map(|o| o.map(|f| f.as_bytes())).map_err(napi_err)
    }

    #[napi]
    pub fn recv_audio_blocking(&self, session_id: String, timeout_ms: Option<u32>) -> Result<Option<AudioFrame>> {
        self.inner.recv_audio_blocking(&session_id, timeout_ms.unwrap_or(20) as u64).map(|o| o.map(AudioFrame::from_rust)).map_err(napi_err)
    }

    #[napi]
    pub fn recv_audio_bytes_blocking(&self, session_id: String, timeout_ms: Option<u32>) -> Result<Option<Vec<u8>>> {
        self.inner.recv_audio_blocking(&session_id, timeout_ms.unwrap_or(20) as u64).map(|o| o.map(|f| f.as_bytes())).map_err(napi_err)
    }

    /// Receive audio as raw bytes, non-blocking Promise. Runs on libuv thread pool.
    #[napi(ts_return_type = "Promise<Buffer | null>")]
    pub fn recv_audio_bytes_async(&self, session_id: String, timeout_ms: Option<u32>) -> Result<AsyncTask<RecvAudioTask>> {
        let rx = self.inner.incoming_rx(&session_id).map_err(napi_err)?;
        Ok(AsyncTask::new(RecvAudioTask { rx, timeout_ms: timeout_ms.unwrap_or(20) as u64 }))
    }

    /// Wait for playout, non-blocking Promise. Runs on libuv thread pool.
    #[napi(ts_return_type = "Promise<boolean>")]
    pub fn wait_for_playout_async(&self, session_id: String, timeout_ms: Option<u32>) -> Result<AsyncTask<WaitForPlayoutTask>> {
        let notify = self.inner.checkpoint_notify(&session_id).map_err(napi_err)?;
        Ok(AsyncTask::new(WaitForPlayoutTask { notify, timeout_ms: timeout_ms.unwrap_or(5000) as u64 }))
    }

    #[napi]
    pub fn mute(&self, session_id: String) -> Result<()> { self.inner.mute(&session_id).map_err(napi_err) }

    #[napi]
    pub fn unmute(&self, session_id: String) -> Result<()> { self.inner.unmute(&session_id).map_err(napi_err) }

    #[napi]
    pub fn pause(&self, session_id: String) -> Result<()> { self.inner.pause(&session_id).map_err(napi_err) }

    #[napi]
    pub fn resume(&self, session_id: String) -> Result<()> { self.inner.resume(&session_id).map_err(napi_err) }

    #[napi]
    pub fn clear_buffer(&self, session_id: String) -> Result<()> { self.inner.clear_buffer(&session_id).map_err(napi_err) }

    #[napi]
    pub fn flush(&self, session_id: String) -> Result<()> { self.inner.flush(&session_id).map_err(napi_err) }

    #[napi]
    pub fn wait_for_playout(&self, session_id: String, timeout_ms: Option<u32>) -> Result<bool> {
        self.inner.wait_for_playout(&session_id, timeout_ms.unwrap_or(5000) as u64).map_err(napi_err)
    }

    #[napi]
    pub fn checkpoint(&self, session_id: String, name: Option<String>) -> Result<String> {
        self.inner.checkpoint(&session_id, name.as_deref()).map_err(napi_err)
    }

    #[napi]
    pub fn send_dtmf(&self, session_id: String, digits: String) -> Result<()> {
        self.inner.send_dtmf(&session_id, &digits).map_err(napi_err)
    }

    #[napi]
    pub fn queued_frames(&self, session_id: String) -> Result<u32> {
        self.inner.queued_frames(&session_id).map(|n| n as u32).map_err(napi_err)
    }

    #[napi]
    pub fn hangup(&self, session_id: String) -> Result<()> { self.inner.hangup(&session_id).map_err(napi_err) }

    #[napi]
    pub fn send_raw_message(&self, session_id: String, message: String) -> Result<()> {
        self.inner.send_raw_message(&session_id, &message).map_err(napi_err)
    }

    #[napi]
    pub fn poll_event(&self) -> Result<Option<EventInfo>> {
        match self.inner.events().try_recv() {
            Ok(event) => Ok(Some(event_to_info(&event))),
            Err(_) => Ok(None),
        }
    }

    #[napi(getter)]
    pub fn sample_rate(&self) -> u32 { self.inner.sample_rate() }

    #[napi(getter)]
    pub fn num_channels(&self) -> u32 { 1 }

    #[napi]
    pub fn shutdown(&self) -> Result<()> { self.inner.shutdown().map_err(napi_err) }
}
