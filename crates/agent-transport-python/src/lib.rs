use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
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
use agent_transport_core::audio_stream::plivo::PlivoProtocol;

mod event_sink;
use event_sink::EventSink;
mod core_endpoint;
use core_endpoint::Core;

fn py_err(e: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(e.to_string())
}

/// Dispatcher thread body — shared by ``SipEndpoint`` and
/// ``AudioStreamEndpoint``. Drains the endpoint's event channel
/// (a ``crossbeam_channel::Receiver``) and invokes the registered
/// Python sink under the GIL, once per event.
///
/// Mirror of LiveKit's ``ffi_event_callback`` in
/// ``livekit/rtc/_ffi_client.py:152-190``: a single constrained Python
/// callable invoked from a thread that owns the FFI event source. The
/// sink contract (see ``adapters/agent_transport/_event_sink.py``) is
/// that the body does only ``loop.call_soon_threadsafe`` into an
/// asyncio Queue — no Rust re-entry, no Rust mutex acquisition. So
/// even though we hold the GIL during ``cb.call1``, no deadlock with
/// any other Rust thread is possible.
///
/// On ``stop`` set, the loop exits after the next ``recv_timeout``
/// returns. ``recv_timeout`` of 100ms bounds shutdown latency.
fn dispatcher_loop(
    rx: crossbeam_channel::Receiver<EndpointEvent>,
    sink: Arc<EventSink>,
    stop: Arc<AtomicBool>,
) {
    // Surface a stalled/slow dispatcher (or a sink that blocks) BEFORE the
    // bounded event channel starts dropping events at its cap. Warn once per
    // backlog episode (reset when it drains), so a wedged sink is visible in
    // logs instead of silently growing memory or losing completions.
    const HIGH_WATER: usize = 8192;
    let mut warned_backlog = false;
    while !stop.load(Ordering::Relaxed) {
        let depth = rx.len();
        if depth > HIGH_WATER && !warned_backlog {
            eprintln!(
                "agent_transport: event-channel backlog high ({} queued) — the \
                 Python sink/dispatcher is not draining fast enough; events will \
                 be dropped if it reaches the channel cap",
                depth,
            );
            warned_backlog = true;
        } else if warned_backlog && depth < HIGH_WATER / 2 {
            warned_backlog = false;
        }
        match rx.recv_timeout(Duration::from_millis(100)) {
            Ok(event) => {
                // Acquire the GIL FIRST, then briefly lock the sink only to
                // clone the callback, dropping the guard BEFORE `call1`. The
                // dispatcher's acquisition order is therefore GIL-before-lock,
                // matching every #[pymethods] sink-mutator (set_event_sink /
                // poll_event / wait_for_event), which PyO3 invokes with the GIL
                // already held and which then take this same lock. Uniform
                // ordering removes the AB-BA deadlock that the previous
                // lock-then-with_gil form created (it held the MutexGuard alive
                // across `Python::with_gil`, i.e. lock-before-GIL, the inverse
                // of the mutators). The clone is dropped-guard'd so `call1` —
                // which runs arbitrary Python — never executes under the lock.
                Python::attach(|py| {
                    // EventSink::snapshot locks via lock_py_attached (detaches
                    // while waiting for the mutex), clones the callback, and drops
                    // the guard — so call1 never runs under the lock and no thread
                    // ever holds the GIL while blocked on the sink (AB-BA gone).
                    if let Some(cb) = sink.snapshot(py) {
                        let dict = match event_to_dict(py, &event) {
                            Ok(d) => d,
                            Err(_) => return,
                        };
                        // Swallow Python exceptions — a sink-side bug
                        // must not kill the dispatcher.
                        if let Err(e) = cb.call1(py, (dict,)) {
                            e.print(py);
                        }
                    }
                });
            }
            Err(crossbeam_channel::RecvTimeoutError::Timeout) => continue,
            Err(crossbeam_channel::RecvTimeoutError::Disconnected) => break,
        }
    }
}

/// Python-visible AudioFrame matching LiveKit's format.
// skip_from_py_object: this Clone pyclass is never extracted by value (only
// passed as `&AudioFrame`), so opt out of the deprecated auto-FromPyObject
// derive rather than carry an unused extraction path (pyo3 0.28).
#[pyclass(skip_from_py_object)]
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
    fn new(data: Vec<i16>, sample_rate: u32, num_channels: u32) -> PyResult<Self> {
        // M2: validate that data length matches a whole number of samples per
        // channel. Without this, callers passing unbalanced buffers (e.g.
        // 5 samples for 2 channels) would silently truncate to 2 samples per
        // channel and lose the trailing sample, producing audio glitches that
        // are very hard to debug downstream.
        if num_channels == 0 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "num_channels must be > 0",
            ));
        }
        if sample_rate == 0 {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "sample_rate must be > 0",
            ));
        }
        if data.len() % (num_channels as usize) != 0 {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "AudioFrame data length ({}) is not a multiple of num_channels ({})",
                data.len(),
                num_channels
            )));
        }
        let samples_per_channel = data.len() as u32 / num_channels;
        Ok(Self {
            data,
            sample_rate,
            num_channels,
            samples_per_channel,
        })
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
// skip_from_py_object: never extracted by value from Python (Rust produces it,
// Python only reads its attributes), so opt out of the deprecated auto-derive.
#[pyclass(skip_from_py_object)]
#[derive(Clone)]
struct CallSession {
    #[pyo3(get)]
    session_id: String,
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
            session_id: s.session_id,
            call_uuid: s.call_uuid,
            direction: format!("{:?}", s.direction),
            state: format!("{:?}", s.state),
            remote_uri: s.remote_uri,
            local_uri: s.local_uri,
            extra_headers: s.extra_headers,
        }
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
        EndpointEvent::CallRinging { session } => {
            dict.set_item("type", "call_ringing")?;
            dict.set_item("session", CallSession::from(session.clone()).into_pyobject(py)?)?;
        }
        EndpointEvent::CallStateChanged { session } => {
            dict.set_item("type", "call_state")?;
            dict.set_item("session", CallSession::from(session.clone()).into_pyobject(py)?)?;
        }
        EndpointEvent::CallAnswered { session } => {
            dict.set_item("type", "call_answered")?;
            // session_id kept for backwards compat with adapters that
            // previously read it from the `call_media_active` event.
            dict.set_item("session_id", &session.session_id)?;
            dict.set_item("session", CallSession::from(session.clone()).into_pyobject(py)?)?;
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
            dict.set_item("session_id", call_id)?;
            dict.set_item("digit", digit.to_string())?;
            dict.set_item("method", method)?;
        }
        EndpointEvent::BeepDetected {
            call_id,
            frequency_hz,
            duration_ms,
        } => {
            dict.set_item("type", "beep_detected")?;
            dict.set_item("session_id", call_id)?;
            dict.set_item("frequency_hz", frequency_hz)?;
            dict.set_item("duration_ms", duration_ms)?;
        }
        EndpointEvent::BeepTimeout { call_id } => {
            dict.set_item("type", "beep_timeout")?;
            dict.set_item("session_id", call_id)?;
        }
        EndpointEvent::Shutdown => {
            dict.set_item("type", "shutdown")?;
        }
        EndpointEvent::AudioCaptureComplete {
            session_id,
            async_id,
            cancelled,
        } => {
            dict.set_item("type", "audio_capture_complete")?;
            dict.set_item("session_id", session_id)?;
            dict.set_item("async_id", async_id)?;
            dict.set_item("cancelled", cancelled)?;
        }
        EndpointEvent::AudioPlayoutComplete {
            session_id,
            async_id,
        } => {
            dict.set_item("type", "audio_playout_complete")?;
            dict.set_item("session_id", session_id)?;
            dict.set_item("async_id", async_id)?;
        }
        EndpointEvent::AudioBufferDrained {
            session_id,
            async_id,
        } => {
            dict.set_item("type", "audio_buffer_drained")?;
            dict.set_item("session_id", session_id)?;
            dict.set_item("async_id", async_id)?;
        }
        EndpointEvent::AudioCaptureError {
            session_id,
            async_id,
            error,
        } => {
            dict.set_item("type", "audio_capture_error")?;
            dict.set_item("session_id", session_id)?;
            dict.set_item("async_id", async_id)?;
            dict.set_item("error", error)?;
        }
    }
    Ok(dict)
}

/// SIP endpoint — call control and audio I/O.
#[pyclass]
struct SipEndpoint {
    inner: Core<RustSipEndpoint>,
    /// LiveKit-style constrained event sink. When a Python callable is
    /// registered, the dispatcher thread drains ``inner.events()`` and
    /// invokes the callable under the GIL. See ``set_event_sink`` for the
    /// contract. ``Arc<Mutex<Option<Py<PyAny>>>>`` so the dispatcher
    /// thread can hold a clone independently of the pyclass lifetime.
    event_sink: Arc<EventSink>,
    /// Signals the dispatcher thread to exit on endpoint drop/shutdown.
    dispatcher_stop: Arc<AtomicBool>,
    /// JoinHandle for the dispatcher thread (so we can wait on it during
    /// shutdown). Wrapped in Mutex<Option<...>> for interior mutability.
    dispatcher_handle: Arc<Mutex<Option<thread::JoinHandle<()>>>>,
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
    #[pyo3(signature = (sip_server="phone.plivo.com", stun_server="stun-fb.plivo.com:3478", codecs=None, log_level=3, input_sample_rate=8000, output_sample_rate=8000, jitter_buffer=false, plc=false, comfort_noise=false))]
    fn new(
        sip_server: &str,
        stun_server: &str,
        codecs: Option<Vec<String>>,
        log_level: u32,
        input_sample_rate: u32,
        output_sample_rate: u32,
        jitter_buffer: bool,
        plc: bool,
        comfort_noise: bool,
    ) -> PyResult<Self> {
        let codec_list = codecs
            .unwrap_or_else(|| vec!["pcmu".into(), "pcma".into()])
            .iter()
            .filter_map(|c| match c.to_lowercase().as_str() {
                "pcmu" => Some(RustCodec::PCMU),
                "pcma" => Some(RustCodec::PCMA),
                _ => None,
            })
            .collect();

        let config = RustEndpointConfig {
            sip_server: sip_server.into(),
            stun_server: stun_server.into(),
            codecs: codec_list,
            log_level,
            input_sample_rate,
            output_sample_rate,
            audio_processing: agent_transport_core::AudioProcessingConfig {
                jitter_buffer,
                plc,
                comfort_noise,
                ..Default::default()
            },
            ..Default::default()
        };

        let inner = Core::new(RustSipEndpoint::new(config)
            .map_err(py_err)?);

        Ok(Self {
            inner,
            event_sink: Arc::new(EventSink::new()),
            dispatcher_stop: Arc::new(AtomicBool::new(false)),
            dispatcher_handle: Arc::new(Mutex::new(None)),
        })
    }

    /// Register with the SIP server. Releases GIL (blocks on SIP signaling).
    fn register(&self, py: Python, username: &str, password: &str) -> PyResult<()> {
        let u = username.to_string();
        let p = password.to_string();
        self.inner.with(py, move |c| c.register(&u, &p)).map_err(py_err)
    }

    /// Unregister. Releases GIL.
    fn unregister(&self, py: Python) -> PyResult<()> {
        self.inner.with(py, |c| c.unregister()).map_err(py_err)
    }

    /// Check registration status. Releases the GIL — `inner.is_registered()`
    /// takes the core `state` Mutex; holding the GIL across it would stall the
    /// asyncio loop whenever that lock is contended (the A.1 GIL-stall class;
    /// this method was missing from the 2_plan.md audit).
    fn is_registered(&self, py: Python) -> bool {
        self.inner.with(py, |c| c.is_registered())
    }

    /// Make an outbound call. Returns session_id. Releases GIL (blocks on SIP signaling).
    /// `from_uri` sets the SIP From header (e.g. "sip:+15551234567@provider.com").
    /// If None, uses the registered contact URI.
    #[pyo3(signature = (dest_uri, from_uri=None, headers=None, session_id=None))]
    fn call(&self, py: Python, dest_uri: &str, from_uri: Option<&str>, headers: Option<HashMap<String, String>>, session_id: Option<String>) -> PyResult<String> {
        let uri = dest_uri.to_string();
        let from = from_uri.map(|s| s.to_string());
        self.inner.with(py, move |c| c.call_with_from(&uri, from.as_deref(), headers, session_id)).map_err(py_err)
    }

    /// Answer an incoming call. Releases GIL.
    #[pyo3(signature = (session_id, code=200))]
    fn answer(&self, py: Python, session_id: &str, code: u16) -> PyResult<()> {
        self.inner.with(py, move |c| c.answer(session_id, code)).map_err(py_err)
    }

    /// Reject an incoming call. Releases GIL.
    #[pyo3(signature = (session_id, code=486))]
    fn reject(&self, py: Python, session_id: &str, code: u16) -> PyResult<()> {
        self.inner.with(py, move |c| c.reject(session_id, code)).map_err(py_err)
    }

    /// Hang up an active call. Releases GIL.
    fn hangup(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.hangup(session_id)).map_err(py_err)
    }

    /// Send DTMF digits. Releases GIL.
    #[pyo3(signature = (session_id, digits, method="rfc2833"))]
    fn send_dtmf(&self, py: Python, session_id: &str, digits: &str, method: &str) -> PyResult<()> {
        let d = digits.to_string();
        let m = method.to_string();
        self.inner.with(py, move |c| c.send_dtmf_with_method(session_id, &d, &m)).map_err(py_err)
    }

    /// Blind transfer via SIP REFER. Releases GIL.
    fn transfer(&self, py: Python, session_id: &str, dest_uri: &str) -> PyResult<()> {
        let uri = dest_uri.to_string();
        self.inner.with(py, move |c| c.transfer(session_id, &uri)).map_err(py_err)
    }

    /// Attended transfer (connect two calls). Releases GIL.
    fn transfer_attended(&self, py: Python, session_id: &str, target_session_id: &str) -> PyResult<()> {
        { let s = session_id.to_string(); let t = target_session_id.to_string(); self.inner.with(py, move |c| c.transfer_attended(&s, &t)).map_err(py_err) }
    }

    /// Send a SIP INFO message. Releases GIL.
    #[pyo3(signature = (session_id, content_type="application/json", body=""))]
    fn send_info(&self, py: Python, session_id: &str, content_type: &str, body: &str) -> PyResult<()> {
        let ct = content_type.to_string();
        let b = body.to_string();
        self.inner.with(py, move |c| c.send_info(session_id, &ct, &b)).map_err(py_err)
    }

    /// Mute outgoing audio. Releases GIL during mutex ops.
    fn mute(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.mute(session_id)).map_err(py_err)
    }

    /// Unmute outgoing audio. Releases GIL during mutex ops.
    fn unmute(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.unmute(session_id)).map_err(py_err)
    }

    /// SIP hold — send Re-INVITE with a=sendonly. Releases GIL.
    fn hold(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.hold(session_id)).map_err(py_err)
    }

    /// SIP unhold — send Re-INVITE with a=sendrecv. Releases GIL.
    fn unhold(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.unhold(session_id)).map_err(py_err)
    }

    /// Send an audio frame (simple, no backpressure callback). Releases GIL during mutex ops.
    fn send_audio(&self, py: Python, session_id: &str, frame: &AudioFrame) -> PyResult<()> {
        let f = frame.to_rust();
        self.inner.with(py, move |c| c.send_audio(session_id, &f)).map_err(py_err)
    }

    /// Push audio frame and return the async_id to await on the endpoint's
    /// event channel.
    ///
    /// **Always returns the async_id** — Python MUST always await
    /// `AudioCaptureComplete { async_id }` (or `AudioCaptureError` on
    /// cancel/flush/drop) via the endpoint's event broker. Mirrors
    /// LiveKit's `capture_audio_frame` invariant
    /// (`livekit/rtc/audio_source.py:142-149`): every request produces
    /// exactly one matching completion event.
    ///
    /// Callers MUST `subscribe(filter_fn=...)` to the event broker
    /// BEFORE calling this method — the immediate-emit path can fire
    /// before this returns, and an unsubscribed event would be lost.
    fn send_audio_async(&self, py: Python, session_id: &str, audio: &[u8], sample_rate: u32, num_channels: u32) -> PyResult<u64> {
        let frame = RustAudioFrame::from_bytes(audio, sample_rate, num_channels);
        self.inner.with(py, move |c| c.send_audio_async(session_id, &frame))
            .map_err(py_err)
    }

    /// Send raw PCM bytes (simple, no backpressure callback). Releases GIL during mutex ops.
    fn send_audio_bytes(&self, py: Python, session_id: &str, audio: &[u8], sample_rate: u32, num_channels: u32) -> PyResult<()> {
        let frame = RustAudioFrame::from_bytes(audio, sample_rate, num_channels);
        self.inner.with(py, move |c| c.send_audio(session_id, &frame)).map_err(py_err)
    }

    /// Send background audio to be mixed with agent voice in the RTP send loop.
    /// Releases GIL during mutex acquisition — critical for high-frequency
    /// background-audio paths (e.g., LiveKit `BackgroundAudioPlayer` running
    /// at ~50 fps).
    fn send_background_audio(&self, py: Python, session_id: &str, audio: &[u8], sample_rate: u32, num_channels: u32) -> PyResult<()> {
        let frame = RustAudioFrame::from_bytes(audio, sample_rate, num_channels);
        self.inner.with(py, move |c| c.send_background_audio(session_id, &frame))
            .map_err(py_err)
    }

    /// Receive an audio frame (non-blocking, returns None if no frame ready).
    /// Releases GIL during mutex ops.
    fn recv_audio(&self, py: Python, session_id: &str) -> PyResult<Option<AudioFrame>> {
        self.inner.with(py, move |c| c.recv_audio(session_id))
            .map(|opt| opt.map(AudioFrame::from_rust))
            .map_err(py_err)
    }

    /// Receive audio as raw PCM bytes (little-endian int16). No Python list conversion.
    /// Returns (bytes, sample_rate, num_channels) or None. Releases GIL during mutex ops.
    fn recv_audio_bytes(&self, py: Python, session_id: &str) -> PyResult<Option<(Vec<u8>, u32, u32)>> {
        self.inner.with(py, move |c| c.recv_audio(session_id))
            .map(|opt| opt.map(|f| (f.as_bytes(), f.sample_rate, f.num_channels)))
            .map_err(py_err)
    }

    /// Receive an audio frame, blocking until one is available or timeout.
    /// Releases the GIL while waiting — safe for high concurrency.
    /// Use this instead of polling recv_audio() in a loop.
    #[pyo3(signature = (session_id, timeout_ms=20))]
    fn recv_audio_blocking(&self, py: Python, session_id: &str, timeout_ms: u64) -> PyResult<Option<AudioFrame>> {
        self.inner.with(py, |c| {
            c.recv_audio_blocking(session_id, timeout_ms)
                .map(|opt| opt.map(AudioFrame::from_rust))
        }).map_err(py_err)
    }

    /// Receive audio as raw bytes, blocking until available. Releases GIL.
    /// Returns (bytes, sample_rate, num_channels) or None.
    /// This is the fastest path for Pipecat/LiveKit adapters.
    #[pyo3(signature = (session_id, timeout_ms=20))]
    fn recv_audio_bytes_blocking(&self, py: Python, session_id: &str, timeout_ms: u64) -> PyResult<Option<(Vec<u8>, u32, u32)>> {
        self.inner.with(py, |c| {
            c.recv_audio_blocking(session_id, timeout_ms)
                .map(|opt| opt.map(|f| (f.as_bytes(), f.sample_rate, f.num_channels)))
        }).map_err(py_err)
    }

    /// Number of audio frames queued for sending (outgoing buffer depth).
    /// Multiply by 0.02 to get queued duration in seconds (each frame = 20ms).
    /// Releases GIL during mutex ops.
    fn queued_frames(&self, py: Python, session_id: &str) -> PyResult<usize> {
        self.inner.with(py, move |c| c.queued_frames(session_id)).map_err(py_err)
    }

    /// Get queued audio duration in milliseconds (real buffer state).
    /// Matches WebRTC's audioSource.queuedDuration. Releases GIL during mutex ops.
    fn queued_duration_ms(&self, py: Python, session_id: &str) -> PyResult<f64> {
        self.inner.with(py, move |c| c.queued_duration_ms(session_id)).map_err(py_err)
    }

    /// Register an async_id to be notified when the audio buffer drains to empty.
    ///
    /// **Always returns the async_id** — Python MUST always await
    /// `AudioPlayoutComplete { async_id }` (or `AudioCaptureError` on
    /// cancel/flush/drop) via the endpoint's event broker. The
    /// completion event always fires (immediately if buffer already
    /// empty, deferred if not). Multiple concurrent waiters supported.
    /// Pause-aware (RTP loop doesn't drain while paused).
    fn wait_for_playout_async(&self, py: Python, session_id: &str) -> PyResult<u64> {
        self.inner.with(py, move |c| c.wait_for_playout_async(session_id))
            .map_err(py_err)
    }

    /// Input audio sample rate in Hz.
    #[getter]
    fn input_sample_rate(&self, py: Python) -> u32 {
        self.inner.with(py, |c| c.input_sample_rate())
    }

    /// Output audio sample rate in Hz.
    #[getter]
    fn output_sample_rate(&self, py: Python) -> u32 {
        self.inner.with(py, |c| c.output_sample_rate())
    }

    /// Number of audio channels (always 1 = mono).
    #[getter]
    fn num_channels(&self) -> u32 {
        1
    }

    /// Start recording a call to a WAV file (stereo by default: L=user, R=agent).
    /// Releases GIL during mutex ops.
    #[pyo3(signature = (session_id, path, stereo=true))]
    fn start_recording(&self, py: Python, session_id: &str, path: &str, stereo: bool) -> PyResult<()> {
        self.inner.with(py, move |c| c.start_recording(session_id, path, stereo)).map_err(py_err)
    }

    /// Stop recording a call. Releases GIL during mutex ops.
    fn stop_recording(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.stop_recording(session_id)).map_err(py_err)
    }

    /// Start async beep detection on a call. Releases GIL during mutex ops.
    #[pyo3(signature = (session_id, timeout_ms=30000, min_duration_ms=80, max_duration_ms=5000))]
    fn detect_beep(
        &self,
        py: Python,
        session_id: String,
        timeout_ms: u32,
        min_duration_ms: u32,
        max_duration_ms: u32,
    ) -> PyResult<()> {
        let sample_rate = self.inner.with(py, |c| c.input_sample_rate());
        let config = RustBeepConfig {
            sample_rate,
            timeout_ms,
            min_duration_ms,
            max_duration_ms,
            ..Default::default()
        };
        self.inner.with(py, move |c| c.detect_beep(&session_id, config)).map_err(py_err)
    }

    /// Cancel beep detection on a call. Releases GIL during mutex ops.
    fn cancel_beep_detection(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.cancel_beep_detection(session_id)).map_err(py_err)
    }

    /// Mark the current playback segment as complete. Releases GIL during mutex ops.
    fn flush(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.flush(session_id)).map_err(py_err)
    }

    /// Clear all queued outgoing audio immediately (barge-in / interruption).
    /// Releases GIL during mutex ops.
    fn clear_buffer(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.clear_buffer(session_id)).map_err(py_err)
    }

    /// Block until all queued audio finishes playing. Releases GIL.
    #[pyo3(signature = (session_id, timeout_ms=5000))]
    fn wait_for_playout(&self, py: Python, session_id: &str, timeout_ms: u64) -> PyResult<bool> {
        self.inner.with(py, |c| c.wait_for_playout(session_id, timeout_ms)).map_err(py_err)
    }

    /// Pause audio playback. Releases GIL during mutex ops.
    fn pause(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.pause(session_id)).map_err(py_err)
    }

    /// Resume audio playback. Releases GIL during mutex ops.
    fn resume(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.resume(session_id)).map_err(py_err)
    }

    /// Register a Python event sink. When set, the endpoint spawns a
    /// dispatcher thread that drains ``inner.events()`` and invokes the
    /// sink (with the GIL held) once per event. The sink body MUST be
    /// non-blocking, non-locking, and MUST NOT re-enter Rust.
    ///
    /// Mirrors LiveKit's ``ffi_event_callback`` registered via
    /// ``livekit_ffi_initialize`` — same architectural shape: a single
    /// constrained Python callable invoked from a thread that owns the
    /// crossbeam receiver, doing only ``call_soon_threadsafe`` into an
    /// asyncio Queue. Replaces the executor-based ``wait_for_event``
    /// pump, eliminating the 2-3 extra asyncio loop ticks per event.
    ///
    /// Idempotent: a second call replaces the sink without re-spawning
    /// the thread. ``None`` clears the sink (events fall through to
    /// ``wait_for_event``/``poll_event`` as before).
    #[pyo3(signature = (callback=None))]
    fn set_event_sink(&self, py: Python, callback: Option<Py<PyAny>>) -> PyResult<()> {
        self.event_sink.set(py, callback);
        // Spawn dispatcher if not already running. Spawning the FIRST time
        // a sink is set keeps the cost off the new() path for users who
        // don't use the sink.
        let mut handle_slot = self
            .dispatcher_handle
            .lock()
            .map_err(|_| py_err("dispatcher handle lock poisoned"))?;
        if handle_slot.is_none() {
            let rx = self.inner.with(py, |c| c.events());
            let sink = self.event_sink.clone();
            let stop = self.dispatcher_stop.clone();
            *handle_slot = Some(thread::spawn(move || dispatcher_loop(rx, sink, stop)));
        }
        Ok(())
    }

    /// Poll for the next event (non-blocking). Returns a dict or None.
    ///
    /// **Deprecated when a sink is registered** — the dispatcher thread
    /// owns the receiver exclusively, so this returns ``None`` to avoid
    /// the dual-consumer race that hit prod pre-Phase-B. Kept functional
    /// when no sink is set (CLI examples still rely on it).
    fn poll_event(&self, py: Python) -> PyResult<Option<Py<PyAny>>> {
        if self.event_sink.is_set(py) {
            return Ok(None);
        }
        match self.inner.with(py, |c| c.events().try_recv()) {
            Ok(event) => {
                let dict = event_to_dict(py, &event)?;
                Ok(Some(dict.into()))
            }
            Err(_) => Ok(None),
        }
    }

    /// Block until an event is received. Returns a dict.
    /// Timeout in milliseconds (0 = wait forever).
    ///
    /// **Deprecated when a sink is registered** — see ``poll_event``.
    #[pyo3(signature = (timeout_ms=0))]
    fn wait_for_event(&self, py: Python, timeout_ms: u64) -> PyResult<Option<Py<PyAny>>> {
        // Check sink presence and RELEASE the event_sink guard BEFORE
        // allow_threads. Holding the guard across allow_threads (which drops the
        // GIL) gives this method an event_sink->GIL ordering that deadlocks the
        // dispatcher_loop / set_event_sink / poll_event (all GIL->event_sink) via
        // AB-BA: the dispatcher holds the GIL and waits on event_sink while this
        // holds event_sink and waits to re-acquire the GIL. The `.lock()`
        // temporary is dropped at the end of this statement, before allow_threads.
        // Sleep briefly to avoid busy-looping callers that still poll after
        // registering a sink.
        let sink_active = self.event_sink.is_set(py);
        if sink_active {
            py.detach(|| thread::sleep(Duration::from_millis(timeout_ms.max(10).min(1000))));
            return Ok(None);
        }
        let rx = self.inner.with(py, |c| c.events());
        let result = if timeout_ms == 0 {
            py.detach(|| rx.recv().ok())
        } else {
            py.detach(|| rx.recv_timeout(Duration::from_millis(timeout_ms)).ok())
        };
        match result {
            Some(event) => {
                let dict = event_to_dict(py, &event)?;
                Ok(Some(dict.into()))
            }
            None => Ok(None),
        }
    }

    /// Shut down the endpoint. Stops dispatcher thread + tears down SIP stack.
    /// Releases GIL.
    fn shutdown(&self, py: Python) -> PyResult<()> {
        // Stop dispatcher first so the inner shutdown() path doesn't race
        // with the dispatcher trying to invoke a sink that may already
        // have been GC'd.
        self.dispatcher_stop.store(true, Ordering::Relaxed);
        if let Ok(mut slot) = self.dispatcher_handle.lock() {
            if let Some(handle) = slot.take() {
                // Best-effort join — don't hold the GIL while waiting.
                py.detach(|| {
                    let _ = handle.join();
                });
            }
        }
        self.inner.with(py, |c| c.shutdown()).map_err(py_err)
    }
}

/// Plivo WebSocket audio streaming endpoint.
#[pyclass]
struct AudioStreamEndpoint {
    inner: Core<RustAudioStreamEndpoint>,
    /// See ``SipEndpoint.event_sink`` — same architectural shape.
    event_sink: Arc<EventSink>,
    dispatcher_stop: Arc<AtomicBool>,
    dispatcher_handle: Arc<Mutex<Option<thread::JoinHandle<()>>>>,
}

#[pymethods]
impl AudioStreamEndpoint {
    #[new]
    #[pyo3(signature = (listen_addr="0.0.0.0:8080", plivo_auth_id="", plivo_auth_token="", input_sample_rate=8000, output_sample_rate=8000, auto_hangup=true))]
    fn new(listen_addr: &str, plivo_auth_id: &str, plivo_auth_token: &str, input_sample_rate: u32, output_sample_rate: u32, auto_hangup: bool) -> PyResult<Self> {
        let config = RustAudioStreamConfig {
            listen_addr: listen_addr.into(), input_sample_rate, output_sample_rate, auto_hangup,
        };
        let protocol = std::sync::Arc::new(PlivoProtocol::new(plivo_auth_id.into(), plivo_auth_token.into()));
        let inner = Core::new(RustAudioStreamEndpoint::new(config, protocol).map_err(py_err)?);
        Ok(Self {
            inner,
            event_sink: Arc::new(EventSink::new()),
            dispatcher_stop: Arc::new(AtomicBool::new(false)),
            dispatcher_handle: Arc::new(Mutex::new(None)),
        })
    }

    /// See ``SipEndpoint.set_event_sink``.
    #[pyo3(signature = (callback=None))]
    fn set_event_sink(&self, py: Python, callback: Option<Py<PyAny>>) -> PyResult<()> {
        self.event_sink.set(py, callback);
        let mut handle_slot = self
            .dispatcher_handle
            .lock()
            .map_err(|_| py_err("dispatcher handle lock poisoned"))?;
        if handle_slot.is_none() {
            let rx = self.inner.with(py, |c| c.events());
            let sink = self.event_sink.clone();
            let stop = self.dispatcher_stop.clone();
            *handle_slot = Some(thread::spawn(move || dispatcher_loop(rx, sink, stop)));
        }
        Ok(())
    }

    /// Send an audio frame. Releases GIL during mutex ops.
    fn send_audio(&self, py: Python, session_id: &str, frame: &AudioFrame) -> PyResult<()> {
        let f = frame.to_rust();
        self.inner.with(py, move |c| c.send_audio(session_id, &f)).map_err(py_err)
    }

    /// Send raw PCM bytes. Releases GIL during mutex ops.
    fn send_audio_bytes(&self, py: Python, session_id: &str, audio: &[u8], sample_rate: u32, num_channels: u32) -> PyResult<()> {
        let frame = RustAudioFrame::from_bytes(audio, sample_rate, num_channels);
        self.inner.with(py, move |c| c.send_audio(session_id, &frame)).map_err(py_err)
    }

    /// Push audio frame and return the async_id to await on the endpoint's
    /// event channel.
    ///
    /// **Always returns the async_id** — Python MUST always await
    /// `AudioCaptureComplete { async_id }` (or `AudioCaptureError` on
    /// cancel/flush/drop) via the endpoint's event broker. Mirrors
    /// LiveKit's `capture_audio_frame` invariant
    /// (`livekit/rtc/audio_source.py:142-149`): every request produces
    /// exactly one matching completion event.
    ///
    /// Callers MUST `subscribe(filter_fn=...)` to the event broker
    /// BEFORE calling this method — the immediate-emit path can fire
    /// before this returns, and an unsubscribed event would be lost.
    fn send_audio_async(&self, py: Python, session_id: &str, audio: &[u8], sample_rate: u32, num_channels: u32) -> PyResult<u64> {
        let frame = RustAudioFrame::from_bytes(audio, sample_rate, num_channels);
        self.inner.with(py, move |c| c.send_audio_async(session_id, &frame))
            .map_err(py_err)
    }

    /// Send background audio to be mixed with agent voice in the send loop.
    /// Used internally by publish_track (background audio, hold music).
    ///
    /// Releases GIL during mutex acquisition — critical for the
    /// `BackgroundAudioPlayer(thinking_sound=...)` path which calls this
    /// at ~50 fps from the main asyncio thread. Pre-0.2.0 this method
    /// held the GIL while acquiring the sessions Mutex, which (combined
    /// with the AudioBuffer Drop firing Python callbacks under that same
    /// Mutex) was the proximate cause of the prod deadlock.
    fn send_background_audio(&self, py: Python, session_id: &str, audio: &[u8], sample_rate: u32, num_channels: u32) -> PyResult<()> {
        let frame = RustAudioFrame::from_bytes(audio, sample_rate, num_channels);
        self.inner.with(py, move |c| c.send_background_audio(session_id, &frame))
            .map_err(py_err)
    }

    /// Non-blocking receive. Releases GIL during mutex ops.
    fn recv_audio(&self, py: Python, session_id: &str) -> PyResult<Option<AudioFrame>> {
        self.inner.with(py, move |c| c.recv_audio(session_id))
            .map(|opt| opt.map(AudioFrame::from_rust))
            .map_err(py_err)
    }

    /// Non-blocking receive returning raw bytes. Releases GIL during mutex ops.
    fn recv_audio_bytes(&self, py: Python, session_id: &str) -> PyResult<Option<(Vec<u8>, u32, u32)>> {
        self.inner.with(py, move |c| c.recv_audio(session_id))
            .map(|opt| opt.map(|f| (f.as_bytes(), f.sample_rate, f.num_channels)))
            .map_err(py_err)
    }

    #[pyo3(signature = (session_id, timeout_ms=20))]
    fn recv_audio_blocking(&self, py: Python, session_id: &str, timeout_ms: u64) -> PyResult<Option<AudioFrame>> {
        self.inner.with(py, |c| c.recv_audio_blocking(session_id, timeout_ms).map(|opt| opt.map(AudioFrame::from_rust))).map_err(py_err)
    }

    #[pyo3(signature = (session_id, timeout_ms=20))]
    fn recv_audio_bytes_blocking(&self, py: Python, session_id: &str, timeout_ms: u64) -> PyResult<Option<(Vec<u8>, u32, u32)>> {
        self.inner.with(py, |c| c.recv_audio_blocking(session_id, timeout_ms).map(|opt| opt.map(|f| (f.as_bytes(), f.sample_rate, f.num_channels)))).map_err(py_err)
    }

    /// Mute. Releases the GIL — `inner.mute()` takes the sessions Mutex; the
    /// SIP twin (SipEndpoint.mute) was converted but this audio_stream one was
    /// left holding the GIL across that lock (the A.1 GIL-stall class).
    fn mute(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.mute(session_id)).map_err(py_err)
    }

    fn unmute(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.unmute(session_id)).map_err(py_err)
    }

    fn pause(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.pause(session_id)).map_err(py_err)
    }

    fn resume(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.resume(session_id)).map_err(py_err)
    }

    fn clear_buffer(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.clear_buffer(session_id)).map_err(py_err)
    }

    /// Send checkpoint — Plivo responds with playedStream when audio finishes.
    /// Releases GIL during mutex ops.
    #[pyo3(signature = (session_id, name=None))]
    fn checkpoint(&self, py: Python, session_id: &str, name: Option<&str>) -> PyResult<String> {
        self.inner.with(py, move |c| c.checkpoint(session_id, name)).map_err(py_err)
    }

    /// Flush: send checkpoint and mark segment complete. Releases GIL during mutex ops.
    fn flush(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.flush(session_id)).map_err(py_err)
    }

    /// Wait for last checkpoint to be confirmed (playedStream event from Plivo).
    #[pyo3(signature = (session_id, timeout_ms=5000))]
    fn wait_for_playout(&self, py: Python, session_id: &str, timeout_ms: u64) -> PyResult<bool> {
        self.inner.with(py, |c| c.wait_for_playout(session_id, timeout_ms)).map_err(py_err)
    }

    /// Number of audio frames queued for sending. Releases GIL during mutex ops.
    fn queued_frames(&self, py: Python, session_id: &str) -> PyResult<usize> {
        self.inner.with(py, move |c| c.queued_frames(session_id)).map_err(py_err)
    }

    /// Queued audio duration in ms. Releases GIL during mutex ops.
    fn queued_duration_ms(&self, py: Python, session_id: &str) -> PyResult<f64> {
        self.inner.with(py, move |c| c.queued_duration_ms(session_id)).map_err(py_err)
    }

    /// Register an async_id for "buffer drained to empty" notification.
    ///
    /// **Always returns the async_id** — Python MUST always await
    /// `AudioPlayoutComplete { async_id }` (or `AudioCaptureError` on
    /// cancel/flush/drop) via the endpoint's event broker. The
    /// completion event always fires (immediately if buffer already
    /// empty, deferred if not). Multiple concurrent waiters supported.
    /// Pause-aware.
    fn wait_for_playout_async(&self, py: Python, session_id: &str) -> PyResult<u64> {
        self.inner.with(py, move |c| c.wait_for_playout_async(session_id))
            .map_err(py_err)
    }

    /// Send DTMF digits via Plivo audio streaming. Releases GIL during mutex ops.
    fn send_dtmf(&self, py: Python, session_id: &str, digits: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.send_dtmf(session_id, digits)).map_err(py_err)
    }

    /// Start async beep detection on incoming audio for an audio stream session.
    /// Releases GIL during mutex ops.
    #[pyo3(signature = (session_id, timeout_ms=30000, min_duration_ms=80, max_duration_ms=5000))]
    fn detect_beep(
        &self,
        py: Python,
        session_id: String,
        timeout_ms: u32,
        min_duration_ms: u32,
        max_duration_ms: u32,
    ) -> PyResult<()> {
        let sample_rate = self.inner.with(py, |c| c.input_sample_rate());
        let config = RustBeepConfig {
            sample_rate,
            timeout_ms,
            min_duration_ms,
            max_duration_ms,
            ..Default::default()
        };
        self.inner.with(py, move |c| c.detect_beep(&session_id, config)).map_err(py_err)
    }

    /// Cancel beep detection on an audio stream session. Releases GIL during mutex ops.
    fn cancel_beep_detection(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.cancel_beep_detection(session_id)).map_err(py_err)
    }

    /// Hang up via Plivo REST API. Releases GIL (blocks on HTTP request).
    #[pyo3(signature = (session_id, auth_id=None, auth_token=None))]
    fn hangup(&self, py: Python, session_id: &str, auth_id: Option<&str>, auth_token: Option<&str>) -> PyResult<()> {
        self.inner.with(py, move |c| c.hangup_with_auth(session_id, auth_id, auth_token)).map_err(py_err)
    }

    /// Send a raw text message over the WebSocket. Releases GIL during mutex ops.
    fn send_raw_message(&self, py: Python, session_id: &str, message: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.send_raw_message(session_id, message)).map_err(py_err)
    }

    /// Start recording (OGG/Opus stereo). Wired through LiveKit's record=True.
    /// Releases GIL during mutex ops.
    fn start_recording(&self, py: Python, session_id: &str, path: &str, stereo: bool) -> PyResult<()> {
        self.inner.with(py, move |c| c.start_recording(session_id, path, stereo)).map_err(py_err)
    }

    /// Stop recording. Releases GIL during mutex ops.
    fn stop_recording(&self, py: Python, session_id: &str) -> PyResult<()> {
        self.inner.with(py, move |c| c.stop_recording(session_id)).map_err(py_err)
    }

    /// **Deprecated when a sink is registered** — see ``set_event_sink``.
    fn poll_event(&self, py: Python) -> PyResult<Option<Py<PyAny>>> {
        if self.event_sink.is_set(py) {
            return Ok(None);
        }
        match self.inner.with(py, |c| c.events().try_recv()) {
            Ok(event) => { let dict = event_to_dict(py, &event)?; Ok(Some(dict.into())) }
            Err(_) => Ok(None),
        }
    }

    /// **Deprecated when a sink is registered** — see ``set_event_sink``.
    #[pyo3(signature = (timeout_ms=0))]
    fn wait_for_event(&self, py: Python, timeout_ms: u64) -> PyResult<Option<Py<PyAny>>> {
        // See SipEndpoint.wait_for_event: release the event_sink guard BEFORE
        // allow_threads to keep this method GIL->event_sink. Holding it across
        // allow_threads would deadlock the dispatcher_loop via AB-BA.
        let sink_active = self.event_sink.is_set(py);
        if sink_active {
            py.detach(|| thread::sleep(Duration::from_millis(timeout_ms.max(10).min(1000))));
            return Ok(None);
        }
        let rx = self.inner.with(py, |c| c.events());
        let result = if timeout_ms == 0 { py.detach(|| rx.recv().ok()) }
        else { py.detach(|| rx.recv_timeout(Duration::from_millis(timeout_ms)).ok()) };
        match result {
            Some(event) => { let dict = event_to_dict(py, &event)?; Ok(Some(dict.into())) }
            None => Ok(None),
        }
    }

    #[getter]
    fn input_sample_rate(&self, py: Python) -> u32 { self.inner.with(py, |c| c.input_sample_rate()) }

    #[getter]
    fn output_sample_rate(&self, py: Python) -> u32 { self.inner.with(py, |c| c.output_sample_rate()) }

    fn shutdown(&self, py: Python) -> PyResult<()> {
        // Stop dispatcher first so it doesn't race with inner.shutdown().
        self.dispatcher_stop.store(true, Ordering::Relaxed);
        if let Ok(mut slot) = self.dispatcher_handle.lock() {
            if let Some(handle) = slot.take() {
                py.detach(|| { let _ = handle.join(); });
            }
        }
        self.inner.with(py, |c| c.shutdown()).map_err(py_err)
    }
}

/// Initialize Rust tracing with the given log level filter.
/// Call this before creating any endpoints to see Rust-level logs.
///
/// Examples:
///   init_logging("debug")                          # agent-transport debug, rsipstack info
///   init_logging("trace")                          # everything including rsipstack
///   init_logging("agent_transport=debug,rsipstack=debug")  # both at debug
///   init_logging("info")                           # default
///
/// RUST_LOG env var overrides the filter argument.
#[pyfunction]
#[pyo3(signature = (filter="info"))]
fn init_logging(filter: &str) -> PyResult<()> {
    use tracing_subscriber::EnvFilter;
    let raw = std::env::var("RUST_LOG").unwrap_or_else(|_| filter.to_string());
    // Expand shorthand levels to filtered versions that skip DNS/transport noise
    let f = match raw.as_str() {
        "debug" => "agent_transport=debug,rsipstack::transport::stream=debug,rsipstack::transport::tcp=debug,rsipstack::dialog=debug,rsipstack::transaction::transaction=debug,rsipstack=warn,hickory=warn".to_string(),
        "trace" => "agent_transport=trace,rsipstack=debug,hickory=warn".to_string(),
        other => other.to_string(),
    };
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::new(f))
        .with_target(true)
        .with_thread_ids(false)
        .with_file(false)
        .try_init()
        .map_err(|e| PyRuntimeError::new_err(format!("tracing already initialized: {}", e)))
}

#[pymodule]
fn agent_transport(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<SipEndpoint>()?;
    m.add_class::<AudioStreamEndpoint>()?;
    m.add_class::<AudioFrame>()?;
    m.add_class::<CallSession>()?;
    m.add_function(wrap_pyfunction!(init_logging, m)?)?;
    Ok(())
}
