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

use agent_endpoint::{
    AudioFrame as RustAudioFrame, BeepDetectorConfig as RustBeepConfig,
    CallSession as RustCallSession, Codec as RustCodec,
    EndpointConfig as RustEndpointConfig, EndpointEvent, SipEndpoint as RustSipEndpoint,
};

fn napi_err(e: impl std::fmt::Display) -> napi::Error {
    Error::from_reason(e.to_string())
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
    pub call_id: i32,
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
}

/// Structured event info returned by pollEvent() and waitForEvent().
#[napi(object)]
#[derive(Clone)]
pub struct EventInfo {
    pub event_type: String,
    pub call_id: Option<i32>,
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
            call_id: Some(session.call_id),
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
            call_id: Some(session.call_id),
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
            call_id: Some(*call_id),
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
            call_id: Some(session.call_id),
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
            call_id: Some(*call_id),
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
            call_id: Some(*call_id),
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
            call_id: Some(*call_id),
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

/// The main Plivo SIP endpoint.
#[napi]
pub struct PlivoEndpoint {
    inner: RustSipEndpoint,
    callbacks: Arc<Mutex<HashMap<String, Vec<EventTsfn>>>>,
    event_thread_running: Arc<AtomicBool>,
}

#[napi]
impl PlivoEndpoint {
    #[napi(constructor)]
    pub fn new(config: Option<EndpointConfig>) -> Result<Self> {
        let cfg = config.unwrap_or(EndpointConfig {
            sip_server: None,
            stun_server: None,
            codecs: None,
            log_level: None,
        });

        let codec_list = cfg
            .codecs
            .unwrap_or_else(|| vec!["pcmu".into(), "pcma".into()])
            .iter()
            .filter_map(|c| match c.to_lowercase().as_str() {
                "opus" => Some(RustCodec::Opus),
                "pcmu" => Some(RustCodec::PCMU),
                "pcma" => Some(RustCodec::PCMA),
                "g722" => Some(RustCodec::G722),
                _ => None,
            })
            .collect();

        let rust_config = RustEndpointConfig {
            sip_server: cfg
                .sip_server
                .unwrap_or_else(|| "phone.plivo.com".into()),
            stun_server: cfg
                .stun_server
                .unwrap_or_else(|| "stun.plivo.com:3478".into()),
            codecs: codec_list,
            log_level: cfg.log_level.unwrap_or(3),
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
    ) -> Result<i32> {
        self.inner
            .call(&dest_uri, headers)
            .map_err(napi_err)
    }

    #[napi]
    pub fn answer(&self, call_id: i32, code: Option<u32>) -> Result<()> {
        self.inner
            .answer(call_id, code.unwrap_or(200) as u16)
            .map_err(napi_err)
    }

    #[napi]
    pub fn reject(&self, call_id: i32, code: Option<u32>) -> Result<()> {
        self.inner
            .reject(call_id, code.unwrap_or(486) as u16)
            .map_err(napi_err)
    }

    #[napi]
    pub fn hangup(&self, call_id: i32) -> Result<()> {
        self.inner
            .hangup(call_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn send_dtmf(&self, call_id: i32, digits: String, method: Option<String>) -> Result<()> {
        self.inner
            .send_dtmf_with_method(call_id, &digits, method.as_deref().unwrap_or("rfc2833"))
            .map_err(napi_err)
    }

    #[napi]
    pub fn transfer(&self, call_id: i32, dest_uri: String) -> Result<()> {
        self.inner
            .transfer(call_id, &dest_uri)
            .map_err(napi_err)
    }

    #[napi]
    pub fn transfer_attended(&self, call_id: i32, target_call_id: i32) -> Result<()> {
        self.inner
            .transfer_attended(call_id, target_call_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn mute(&self, call_id: i32) -> Result<()> {
        self.inner
            .mute(call_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn unmute(&self, call_id: i32) -> Result<()> {
        self.inner
            .unmute(call_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn send_audio(&self, call_id: i32, frame: AudioFrame) -> Result<()> {
        self.inner
            .send_audio(call_id, &frame.to_rust())
            .map_err(napi_err)
    }

    #[napi]
    pub fn recv_audio(&self, call_id: i32) -> Result<Option<AudioFrame>> {
        self.inner
            .recv_audio(call_id)
            .map(|opt| opt.map(AudioFrame::from_rust))
            .map_err(napi_err)
    }

    #[napi]
    pub fn start_recording(&self, call_id: i32, path: String) -> Result<()> {
        self.inner
            .start_recording(call_id, &path)
            .map_err(napi_err)
    }

    #[napi]
    pub fn stop_recording(&self, call_id: i32) -> Result<()> {
        self.inner
            .stop_recording(call_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn detect_beep(
        &self,
        call_id: i32,
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
            .detect_beep(call_id, config)
            .map_err(napi_err)
    }

    #[napi]
    pub fn cancel_beep_detection(&self, call_id: i32) -> Result<()> {
        self.inner
            .cancel_beep_detection(call_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn flush(&self, call_id: i32) -> Result<()> {
        self.inner
            .flush(call_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn clear_buffer(&self, call_id: i32) -> Result<()> {
        self.inner
            .clear_buffer(call_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn wait_for_playout(&self, call_id: i32, timeout_ms: Option<u32>) -> Result<bool> {
        self.inner
            .wait_for_playout(call_id, timeout_ms.unwrap_or(5000) as u64)
            .map_err(napi_err)
    }

    #[napi]
    pub fn pause(&self, call_id: i32) -> Result<()> {
        self.inner
            .pause(call_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn resume(&self, call_id: i32) -> Result<()> {
        self.inner
            .resume(call_id)
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

impl PlivoEndpoint {
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
