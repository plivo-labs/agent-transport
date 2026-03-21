use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use agent_transport_core::{
    AudioFrame as RustAudioFrame, BeepDetectorConfig as RustBeepConfig,
    CallSession as RustCallSession, Codec as RustCodec,
    EndpointConfig as RustEndpointConfig, EndpointEvent, SipEndpoint as RustSipEndpoint,
};
use agent_transport_core::audio_stream::config::AudioStreamConfig as RustAudioStreamConfig;
use agent_transport_core::audio_stream::endpoint::AudioStreamEndpoint as RustAudioStreamEndpoint;

fn py_err(e: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(e.to_string())
}

/// Python-visible AudioFrame matching LiveKit's format.
#[pyclass]
#[derive(Clone)]
struct AudioFrame {
    #[pyo3(get)]
    data: Vec<i16>,
    #[pyo3(get)]
    sample_rate: u32,
    #[pyo3(get)]
    num_channels: u32,
    #[pyo3(get)]
    samples_per_channel: u32,
}

#[pymethods]
impl AudioFrame {
    #[new]
    fn new(data: Vec<i16>, sample_rate: u32, num_channels: u32) -> Self {
        let samples_per_channel = if num_channels > 0 {
            data.len() as u32 / num_channels
        } else {
            0
        };
        Self {
            data,
            sample_rate,
            num_channels,
            samples_per_channel,
        }
    }

    #[staticmethod]
    fn silence(sample_rate: u32, num_channels: u32, duration_ms: u32) -> Self {
        let f = RustAudioFrame::silence(sample_rate, num_channels, duration_ms);
        Self::from_rust(f)
    }

    fn duration_ms(&self) -> u32 {
        if self.sample_rate == 0 {
            return 0;
        }
        self.samples_per_channel * 1000 / self.sample_rate
    }

    fn as_bytes(&self) -> Vec<u8> {
        self.data.iter().flat_map(|s| s.to_le_bytes()).collect()
    }

    #[staticmethod]
    fn from_bytes(data: Vec<u8>, sample_rate: u32, num_channels: u32) -> Self {
        let f = RustAudioFrame::from_bytes(&data, sample_rate, num_channels);
        Self::from_rust(f)
    }
}

impl AudioFrame {
    fn from_rust(f: RustAudioFrame) -> Self {
        Self {
            data: f.data,
            sample_rate: f.sample_rate,
            num_channels: f.num_channels,
            samples_per_channel: f.samples_per_channel,
        }
    }

    fn to_rust(&self) -> RustAudioFrame {
        RustAudioFrame::new(self.data.clone(), self.sample_rate, self.num_channels)
    }
}

/// Python-visible CallSession.
#[pyclass]
#[derive(Clone)]
struct CallSession {
    #[pyo3(get)]
    call_id: i32,
    #[pyo3(get)]
    call_uuid: Option<String>,
    #[pyo3(get)]
    direction: String,
    #[pyo3(get)]
    state: String,
    #[pyo3(get)]
    remote_uri: String,
    #[pyo3(get)]
    local_uri: String,
    #[pyo3(get)]
    extra_headers: HashMap<String, String>,
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

/// Returned by `ep.on("event_name")` — usable as a decorator.
///
/// ```python
/// @ep.on("incoming_call")
/// def handler(session):
///     ep.answer(session.call_id)
/// ```
#[pyclass]
struct EventDecorator {
    callbacks: Arc<Mutex<HashMap<String, Vec<Py<PyAny>>>>>,
    event_name: String,
}

#[pymethods]
impl EventDecorator {
    fn __call__(&self, py: Python, func: Py<PyAny>) -> PyResult<Py<PyAny>> {
        self.callbacks
            .lock()
            .unwrap()
            .entry(self.event_name.clone())
            .or_default()
            .push(func.clone_ref(py));
        Ok(func)
    }
}

/// Convert an EndpointEvent to a Python dict.
fn event_to_dict<'py>(py: Python<'py>, event: &EndpointEvent) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    match event {
        EndpointEvent::Registered => {
            dict.set_item("type", "registered")?;
        }
        EndpointEvent::RegistrationFailed { error } => {
            dict.set_item("type", "registration_failed")?;
            dict.set_item("error", error)?;
        }
        EndpointEvent::Unregistered => {
            dict.set_item("type", "unregistered")?;
        }
        EndpointEvent::IncomingCall { session } => {
            dict.set_item("type", "incoming_call")?;
            dict.set_item("session", CallSession::from(session.clone()).into_pyobject(py)?)?;
        }
        EndpointEvent::CallStateChanged { session } => {
            dict.set_item("type", "call_state")?;
            dict.set_item("session", CallSession::from(session.clone()).into_pyobject(py)?)?;
        }
        EndpointEvent::CallMediaActive { call_id } => {
            dict.set_item("type", "call_media_active")?;
            dict.set_item("call_id", call_id)?;
        }
        EndpointEvent::CallTerminated { session, reason } => {
            dict.set_item("type", "call_terminated")?;
            dict.set_item("session", CallSession::from(session.clone()).into_pyobject(py)?)?;
            dict.set_item("reason", reason)?;
        }
        EndpointEvent::DtmfReceived {
            call_id,
            digit,
            method,
        } => {
            dict.set_item("type", "dtmf_received")?;
            dict.set_item("call_id", call_id)?;
            dict.set_item("digit", digit.to_string())?;
            dict.set_item("method", method)?;
        }
        EndpointEvent::BeepDetected {
            call_id,
            frequency_hz,
            duration_ms,
        } => {
            dict.set_item("type", "beep_detected")?;
            dict.set_item("call_id", call_id)?;
            dict.set_item("frequency_hz", frequency_hz)?;
            dict.set_item("duration_ms", duration_ms)?;
        }
        EndpointEvent::BeepTimeout { call_id } => {
            dict.set_item("type", "beep_timeout")?;
            dict.set_item("call_id", call_id)?;
        }
    }
    Ok(dict)
}

/// Dispatch an event to registered Python callbacks.
fn dispatch_event(
    py: Python,
    callbacks: &Arc<Mutex<HashMap<String, Vec<Py<PyAny>>>>>,
    event: &EndpointEvent,
) {
    let name = event.callback_name();
    let cbs = callbacks.lock().unwrap();
    let Some(handlers) = cbs.get(name) else {
        return;
    };

    let dict = match event_to_dict(py, event) {
        Ok(d) => d,
        Err(_) => return,
    };

    for handler in handlers {
        if let Err(e) = handler.call1(py, (&dict,)) {
            e.print(py);
        }
    }
}

/// SIP endpoint — call control and audio I/O.
#[pyclass]
struct SipEndpoint {
    inner: RustSipEndpoint,
    callbacks: Arc<Mutex<HashMap<String, Vec<Py<PyAny>>>>>,
    event_thread_running: Arc<AtomicBool>,
}

#[pymethods]
impl SipEndpoint {
    #[new]
    /// Create a new SIP endpoint.
    ///
    /// Audio processing options (requires Cargo features):
    ///   jitter_buffer: Enable adaptive jitter buffer (feature: jitter-buffer)
    ///   plc: Enable packet loss concealment (feature: plc)
    ///   comfort_noise: Enable comfort noise generation (feature: comfort-noise)
    #[pyo3(signature = (sip_server="phone.plivo.com", stun_server="stun-fb.plivo.com:3478", codecs=None, log_level=3, jitter_buffer=false, plc=false, comfort_noise=false))]
    fn new(
        sip_server: &str,
        stun_server: &str,
        codecs: Option<Vec<String>>,
        log_level: u32,
        jitter_buffer: bool,
        plc: bool,
        comfort_noise: bool,
    ) -> PyResult<Self> {
        let codec_list = codecs
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

        let config = RustEndpointConfig {
            sip_server: sip_server.into(),
            stun_server: stun_server.into(),
            codecs: codec_list,
            log_level,
            audio_processing: agent_transport_core::AudioProcessingConfig {
                jitter_buffer,
                plc,
                comfort_noise,
                ..Default::default()
            },
            ..Default::default()
        };

        let inner = RustSipEndpoint::new(config)
            .map_err(py_err)?;

        Ok(Self {
            inner,
            callbacks: Arc::new(Mutex::new(HashMap::new())),
            event_thread_running: Arc::new(AtomicBool::new(false)),
        })
    }

    /// Register an event callback. Can be used as a decorator:
    ///
    /// ```python
    /// @ep.on("incoming_call")
    /// def handler(event):
    ///     print(event["session"].remote_uri)
    ///     ep.answer(event["session"].call_id)
    /// ```
    ///
    /// Or with a direct callback:
    ///
    /// ```python
    /// ep.on("dtmf_received", lambda event: print(event["digit"]))
    /// ```
    ///
    /// Event names: registered, registration_failed, unregistered,
    /// incoming_call, call_state, call_media_active, call_terminated,
    /// dtmf_received, beep_detected, beep_timeout
    #[pyo3(signature = (event_name, callback=None))]
    fn on(
        &self,
        py: Python,
        event_name: String,
        callback: Option<Py<PyAny>>,
    ) -> PyResult<PyObject> {
        if let Some(cb) = callback {
            // Direct registration: ep.on("event", callback)
            self.callbacks
                .lock()
                .unwrap()
                .entry(event_name)
                .or_default()
                .push(cb);
            self.ensure_event_loop();
            Ok(py.None())
        } else {
            // Decorator mode: @ep.on("event")
            self.ensure_event_loop();
            let decorator = EventDecorator {
                callbacks: self.callbacks.clone(),
                event_name,
            };
            Ok(decorator.into_pyobject(py)?.into_any().unbind())
        }
    }

    /// Register with the SIP server.
    fn register(&self, username: &str, password: &str) -> PyResult<()> {
        self.inner
            .register(username, password)
            .map_err(py_err)
    }

    /// Unregister.
    fn unregister(&self) -> PyResult<()> {
        self.inner
            .unregister()
            .map_err(py_err)
    }

    /// Check registration status.
    fn is_registered(&self) -> bool {
        self.inner.is_registered()
    }

    /// Make an outbound call. Returns call_id.
    /// Optional headers dict adds custom SIP headers to the INVITE.
    #[pyo3(signature = (dest_uri, headers=None))]
    fn call(&self, dest_uri: &str, headers: Option<HashMap<String, String>>) -> PyResult<i32> {
        self.inner
            .call(dest_uri, headers)
            .map_err(py_err)
    }

    /// Answer an incoming call.
    #[pyo3(signature = (call_id, code=200))]
    fn answer(&self, call_id: i32, code: u16) -> PyResult<()> {
        self.inner
            .answer(call_id, code)
            .map_err(py_err)
    }

    /// Reject an incoming call.
    #[pyo3(signature = (call_id, code=486))]
    fn reject(&self, call_id: i32, code: u16) -> PyResult<()> {
        self.inner
            .reject(call_id, code)
            .map_err(py_err)
    }

    /// Hang up an active call.
    fn hangup(&self, call_id: i32) -> PyResult<()> {
        self.inner
            .hangup(call_id)
            .map_err(py_err)
    }

    /// Send DTMF digits.
    #[pyo3(signature = (call_id, digits, method="rfc2833"))]
    fn send_dtmf(&self, call_id: i32, digits: &str, method: &str) -> PyResult<()> {
        self.inner
            .send_dtmf_with_method(call_id, digits, method)
            .map_err(py_err)
    }

    /// Blind transfer via SIP REFER.
    fn transfer(&self, call_id: i32, dest_uri: &str) -> PyResult<()> {
        self.inner
            .transfer(call_id, dest_uri)
            .map_err(py_err)
    }

    /// Attended transfer (connect two calls).
    fn transfer_attended(&self, call_id: i32, target_call_id: i32) -> PyResult<()> {
        self.inner
            .transfer_attended(call_id, target_call_id)
            .map_err(py_err)
    }

    /// Mute outgoing audio.
    fn mute(&self, call_id: i32) -> PyResult<()> {
        self.inner
            .mute(call_id)
            .map_err(py_err)
    }

    /// Unmute outgoing audio.
    fn unmute(&self, call_id: i32) -> PyResult<()> {
        self.inner
            .unmute(call_id)
            .map_err(py_err)
    }

    /// Send an audio frame.
    fn send_audio(&self, call_id: i32, frame: &AudioFrame) -> PyResult<()> {
        self.inner
            .send_audio(call_id, &frame.to_rust())
            .map_err(py_err)
    }

    /// Send raw PCM bytes (little-endian int16) directly. No Python list conversion.
    /// Use this from Pipecat/LiveKit adapters for zero-copy performance.
    fn send_audio_bytes(&self, call_id: i32, audio: &[u8], sample_rate: u32, num_channels: u32) -> PyResult<()> {
        let frame = RustAudioFrame::from_bytes(audio, sample_rate, num_channels);
        self.inner.send_audio(call_id, &frame).map_err(py_err)
    }

    /// Receive an audio frame (non-blocking, returns None if no frame ready).
    fn recv_audio(&self, call_id: i32) -> PyResult<Option<AudioFrame>> {
        self.inner
            .recv_audio(call_id)
            .map(|opt| opt.map(AudioFrame::from_rust))
            .map_err(py_err)
    }

    /// Receive audio as raw PCM bytes (little-endian int16). No Python list conversion.
    /// Returns (bytes, sample_rate, num_channels) or None.
    fn recv_audio_bytes(&self, call_id: i32) -> PyResult<Option<(Vec<u8>, u32, u32)>> {
        self.inner
            .recv_audio(call_id)
            .map(|opt| opt.map(|f| (f.as_bytes(), f.sample_rate, f.num_channels)))
            .map_err(py_err)
    }

    /// Receive an audio frame, blocking until one is available or timeout.
    /// Releases the GIL while waiting — safe for high concurrency.
    /// Use this instead of polling recv_audio() in a loop.
    #[pyo3(signature = (call_id, timeout_ms=20))]
    fn recv_audio_blocking(&self, py: Python, call_id: i32, timeout_ms: u64) -> PyResult<Option<AudioFrame>> {
        let inner = &self.inner;
        py.allow_threads(|| {
            inner.recv_audio_blocking(call_id, timeout_ms)
                .map(|opt| opt.map(AudioFrame::from_rust))
        }).map_err(py_err)
    }

    /// Receive audio as raw bytes, blocking until available. Releases GIL.
    /// Returns (bytes, sample_rate, num_channels) or None.
    /// This is the fastest path for Pipecat/LiveKit adapters.
    #[pyo3(signature = (call_id, timeout_ms=20))]
    fn recv_audio_bytes_blocking(&self, py: Python, call_id: i32, timeout_ms: u64) -> PyResult<Option<(Vec<u8>, u32, u32)>> {
        let inner = &self.inner;
        py.allow_threads(|| {
            inner.recv_audio_blocking(call_id, timeout_ms)
                .map(|opt| opt.map(|f| (f.as_bytes(), f.sample_rate, f.num_channels)))
        }).map_err(py_err)
    }

    /// Number of audio frames queued for sending (outgoing buffer depth).
    /// Multiply by 0.02 to get queued duration in seconds (each frame = 20ms).
    fn queued_frames(&self, call_id: i32) -> PyResult<usize> {
        self.inner.queued_frames(call_id).map_err(py_err)
    }

    /// Audio sample rate in Hz (always 16000).
    #[getter]
    fn sample_rate(&self) -> u32 {
        16000
    }

    /// Number of audio channels (always 1 = mono).
    #[getter]
    fn num_channels(&self) -> u32 {
        1
    }

    /// Start recording a call to a WAV file.
    fn start_recording(&self, call_id: i32, path: &str) -> PyResult<()> {
        self.inner
            .start_recording(call_id, path)
            .map_err(py_err)
    }

    /// Stop recording a call.
    fn stop_recording(&self, call_id: i32) -> PyResult<()> {
        self.inner
            .stop_recording(call_id)
            .map_err(py_err)
    }

    /// Start async beep detection on a call.
    #[pyo3(signature = (call_id, timeout_ms=30000, min_duration_ms=80, max_duration_ms=5000))]
    fn detect_beep(
        &self,
        call_id: i32,
        timeout_ms: u32,
        min_duration_ms: u32,
        max_duration_ms: u32,
    ) -> PyResult<()> {
        let config = RustBeepConfig {
            sample_rate: 16000,
            timeout_ms,
            min_duration_ms,
            max_duration_ms,
            ..Default::default()
        };
        self.inner
            .detect_beep(call_id, config)
            .map_err(py_err)
    }

    /// Cancel beep detection on a call.
    fn cancel_beep_detection(&self, call_id: i32) -> PyResult<()> {
        self.inner
            .cancel_beep_detection(call_id)
            .map_err(py_err)
    }

    /// Mark the current playback segment as complete.
    fn flush(&self, call_id: i32) -> PyResult<()> {
        self.inner
            .flush(call_id)
            .map_err(py_err)
    }

    /// Clear all queued outgoing audio immediately (barge-in / interruption).
    fn clear_buffer(&self, call_id: i32) -> PyResult<()> {
        self.inner
            .clear_buffer(call_id)
            .map_err(py_err)
    }

    /// Block until all queued audio finishes playing.
    #[pyo3(signature = (call_id, timeout_ms=5000))]
    fn wait_for_playout(&self, call_id: i32, timeout_ms: u64) -> PyResult<bool> {
        self.inner
            .wait_for_playout(call_id, timeout_ms)
            .map_err(py_err)
    }

    /// Pause audio playback.
    fn pause(&self, call_id: i32) -> PyResult<()> {
        self.inner
            .pause(call_id)
            .map_err(py_err)
    }

    /// Resume audio playback.
    fn resume(&self, call_id: i32) -> PyResult<()> {
        self.inner
            .resume(call_id)
            .map_err(py_err)
    }

    /// Poll for the next event (non-blocking). Returns a dict or None.
    fn poll_event(&self, py: Python) -> PyResult<Option<PyObject>> {
        match self.inner.events().try_recv() {
            Ok(event) => {
                let dict = event_to_dict(py, &event)?;
                Ok(Some(dict.into()))
            }
            Err(_) => Ok(None),
        }
    }

    /// Block until an event is received. Returns a dict.
    /// Timeout in milliseconds (0 = wait forever).
    #[pyo3(signature = (timeout_ms=0))]
    fn wait_for_event(&self, py: Python, timeout_ms: u64) -> PyResult<Option<PyObject>> {
        let rx = self.inner.events();
        let result = if timeout_ms == 0 {
            // Allow other Python threads to run while we block
            py.allow_threads(|| rx.recv().ok())
        } else {
            py.allow_threads(|| rx.recv_timeout(Duration::from_millis(timeout_ms)).ok())
        };
        match result {
            Some(event) => {
                let dict = event_to_dict(py, &event)?;
                Ok(Some(dict.into()))
            }
            None => Ok(None),
        }
    }

    /// Shut down the endpoint. Stops the event loop and tears down SIP stack.
    fn shutdown(&self) -> PyResult<()> {
        self.event_thread_running.store(false, Ordering::Relaxed);
        self.inner
            .shutdown()
            .map_err(py_err)
    }
}

impl SipEndpoint {
    /// Start the background event dispatch thread if not already running.
    fn ensure_event_loop(&self) {
        if self.event_thread_running.swap(true, Ordering::Relaxed) {
            return; // already running
        }

        let rx = self.inner.events();
        let callbacks = self.callbacks.clone();
        let running = self.event_thread_running.clone();

        std::thread::spawn(move || {
            while running.load(Ordering::Relaxed) {
                match rx.recv_timeout(Duration::from_millis(100)) {
                    Ok(event) => {
                        Python::with_gil(|py| {
                            dispatch_event(py, &callbacks, &event);
                        });
                    }
                    Err(crossbeam_channel::RecvTimeoutError::Timeout) => continue,
                    Err(crossbeam_channel::RecvTimeoutError::Disconnected) => break,
                }
            }
        });
    }
}

/// Plivo WebSocket audio streaming endpoint.
#[pyclass]
struct AudioStreamEndpoint {
    inner: RustAudioStreamEndpoint,
}

#[pymethods]
impl AudioStreamEndpoint {
    #[new]
    #[pyo3(signature = (listen_addr="0.0.0.0:8080", plivo_auth_id="", plivo_auth_token="", sample_rate=16000, auto_hangup=true))]
    fn new(listen_addr: &str, plivo_auth_id: &str, plivo_auth_token: &str, sample_rate: u32, auto_hangup: bool) -> PyResult<Self> {
        let config = RustAudioStreamConfig {
            listen_addr: listen_addr.into(), plivo_auth_id: plivo_auth_id.into(),
            plivo_auth_token: plivo_auth_token.into(), sample_rate, auto_hangup,
        };
        let inner = RustAudioStreamEndpoint::new(config).map_err(py_err)?;
        Ok(Self { inner })
    }

    fn send_audio(&self, session_id: i32, frame: &AudioFrame) -> PyResult<()> {
        self.inner.send_audio(session_id, &frame.to_rust()).map_err(py_err)
    }

    fn send_audio_bytes(&self, session_id: i32, audio: &[u8], sample_rate: u32, num_channels: u32) -> PyResult<()> {
        let frame = RustAudioFrame::from_bytes(audio, sample_rate, num_channels);
        self.inner.send_audio(session_id, &frame).map_err(py_err)
    }

    fn recv_audio(&self, session_id: i32) -> PyResult<Option<AudioFrame>> {
        self.inner.recv_audio(session_id).map(|opt| opt.map(AudioFrame::from_rust)).map_err(py_err)
    }

    fn recv_audio_bytes(&self, session_id: i32) -> PyResult<Option<(Vec<u8>, u32, u32)>> {
        self.inner.recv_audio(session_id).map(|opt| opt.map(|f| (f.as_bytes(), f.sample_rate, f.num_channels))).map_err(py_err)
    }

    #[pyo3(signature = (session_id, timeout_ms=20))]
    fn recv_audio_blocking(&self, py: Python, session_id: i32, timeout_ms: u64) -> PyResult<Option<AudioFrame>> {
        let inner = &self.inner;
        py.allow_threads(|| inner.recv_audio_blocking(session_id, timeout_ms).map(|opt| opt.map(AudioFrame::from_rust))).map_err(py_err)
    }

    #[pyo3(signature = (session_id, timeout_ms=20))]
    fn recv_audio_bytes_blocking(&self, py: Python, session_id: i32, timeout_ms: u64) -> PyResult<Option<(Vec<u8>, u32, u32)>> {
        let inner = &self.inner;
        py.allow_threads(|| inner.recv_audio_blocking(session_id, timeout_ms).map(|opt| opt.map(|f| (f.as_bytes(), f.sample_rate, f.num_channels)))).map_err(py_err)
    }

    fn mute(&self, session_id: i32) -> PyResult<()> {
        self.inner.mute(session_id).map_err(py_err)
    }

    fn unmute(&self, session_id: i32) -> PyResult<()> {
        self.inner.unmute(session_id).map_err(py_err)
    }

    fn pause(&self, session_id: i32) -> PyResult<()> {
        self.inner.pause(session_id).map_err(py_err)
    }

    fn resume(&self, session_id: i32) -> PyResult<()> {
        self.inner.resume(session_id).map_err(py_err)
    }

    fn clear_buffer(&self, session_id: i32) -> PyResult<()> {
        self.inner.clear_buffer(session_id).map_err(py_err)
    }

    /// Send checkpoint — Plivo responds with playedStream when audio finishes.
    #[pyo3(signature = (session_id, name=None))]
    fn checkpoint(&self, session_id: i32, name: Option<&str>) -> PyResult<String> {
        self.inner.checkpoint(session_id, name).map_err(py_err)
    }

    /// Flush: send checkpoint and mark segment complete.
    fn flush(&self, session_id: i32) -> PyResult<()> {
        self.inner.flush(session_id).map_err(py_err)
    }

    /// Wait for last checkpoint to be confirmed (playedStream event from Plivo).
    #[pyo3(signature = (session_id, timeout_ms=5000))]
    fn wait_for_playout(&self, py: Python, session_id: i32, timeout_ms: u64) -> PyResult<bool> {
        let inner = &self.inner;
        py.allow_threads(|| inner.wait_for_playout(session_id, timeout_ms)).map_err(py_err)
    }

    fn queued_frames(&self, session_id: i32) -> PyResult<usize> {
        self.inner.queued_frames(session_id).map_err(py_err)
    }

    /// Send DTMF digits via Plivo audio streaming.
    fn send_dtmf(&self, session_id: i32, digits: &str) -> PyResult<()> {
        self.inner.send_dtmf(session_id, digits).map_err(py_err)
    }

    fn hangup(&self, session_id: i32) -> PyResult<()> {
        self.inner.hangup(session_id).map_err(py_err)
    }

    /// Send a raw text message over the WebSocket.
    fn send_raw_message(&self, session_id: i32, message: &str) -> PyResult<()> {
        self.inner.send_raw_message(session_id, message).map_err(py_err)
    }

    fn poll_event(&self, py: Python) -> PyResult<Option<PyObject>> {
        match self.inner.events().try_recv() {
            Ok(event) => { let dict = event_to_dict(py, &event)?; Ok(Some(dict.into())) }
            Err(_) => Ok(None),
        }
    }

    #[pyo3(signature = (timeout_ms=0))]
    fn wait_for_event(&self, py: Python, timeout_ms: u64) -> PyResult<Option<PyObject>> {
        let rx = self.inner.events();
        let result = if timeout_ms == 0 { py.allow_threads(|| rx.recv().ok()) }
        else { py.allow_threads(|| rx.recv_timeout(std::time::Duration::from_millis(timeout_ms)).ok()) };
        match result {
            Some(event) => { let dict = event_to_dict(py, &event)?; Ok(Some(dict.into())) }
            None => Ok(None),
        }
    }

    #[getter]
    fn sample_rate(&self) -> u32 { self.inner.sample_rate() }

    fn shutdown(&self) -> PyResult<()> { self.inner.shutdown().map_err(py_err) }
}

#[pymodule]
fn agent_transport(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<SipEndpoint>()?;
    m.add_class::<AudioStreamEndpoint>()?;
    m.add_class::<AudioFrame>()?;
    m.add_class::<CallSession>()?;
    Ok(())
}
