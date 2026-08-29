use std::collections::HashMap;
use std::sync::Arc;

use napi::bindgen_prelude::*;
use napi_derive::napi;

use agent_transport_core::{
    AudioFrame as RustAudioFrame, BeepDetectorConfig as RustBeepConfig,
    CallSession as RustCallSession, Codec as RustCodec,
    EndpointConfig as RustEndpointConfig, EndpointEvent, SipEndpoint as RustSipEndpoint,
};
use agent_transport_core::audio_stream::config::AudioStreamConfig as RustAudioStreamConfig;
use agent_transport_core::audio_stream::endpoint::AudioStreamEndpoint as RustAudioStreamEndpoint;
use agent_transport_core::audio_stream::plivo::PlivoProtocol;

fn napi_err(e: impl std::fmt::Display) -> napi::Error {
    Error::from_reason(e.to_string())
}

// ─── Async tasks for blocking operations ─────────────────────────────────────

use napi::Task;

/// Lock a Mutex and recover from poisoning instead of panicking. The
/// AsyncTask threads run on napi's thread pool — a panic from a poisoned
/// mutex would propagate as an unhandled error to JS callers and could
/// take down adjacent tasks. Recovery is strictly safer.
fn lock_or_recover<T>(m: &std::sync::Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    match m.lock() {
        Ok(g) => g,
        Err(e) => e.into_inner(),
    }
}

pub struct RecvAudioTask {
    rx: crossbeam_channel::Receiver<agent_transport_core::AudioFrame>,
    timeout_ms: u64,
}

impl Task for RecvAudioTask {
    type Output = Option<Vec<u8>>;
    type JsValue = Option<Buffer>;
    fn compute(&mut self) -> Result<Self::Output> {
        Ok(self.rx.recv_timeout(std::time::Duration::from_millis(self.timeout_ms)).ok().map(|f| f.as_bytes()))
    }
    fn resolve(&mut self, _env: Env, output: Self::Output) -> Result<Self::JsValue> {
        Ok(output.map(|v| Buffer::from(v)))
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
        let guard = lock_or_recover(lock);
        let result = match cvar.wait_timeout_while(
            guard,
            std::time::Duration::from_millis(self.timeout_ms),
            |cp| cp.is_none(),
        ) {
            Ok(r) => r,
            Err(e) => e.into_inner(),
        };
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
        let guard = lock_or_recover(lock);
        let result = match cvar.wait_timeout_while(
            guard,
            std::time::Duration::from_millis(self.timeout_ms),
            |done| !*done,
        ) {
            Ok(r) => r,
            Err(e) => e.into_inner(),
        };
        Ok(!result.1.timed_out())
    }
    fn resolve(&mut self, _env: Env, output: Self::Output) -> Result<Self::JsValue> {
        Ok(output)
    }
}

/// Send DTMF over RTP (RFC 4733), paced on a napi worker thread so the
/// Node main event loop isn't blocked for ~280 ms per digit. Wraps the
/// existing blocking `SipEndpoint::send_dtmf_with_method` which internally
/// sends 14 RTP packets × 20 ms ptime per digit.
pub struct SendDtmfTask {
    endpoint: Arc<RustSipEndpoint>,
    session_id: String,
    digits: String,
    method: String,
}

impl Task for SendDtmfTask {
    type Output = ();
    type JsValue = ();
    fn compute(&mut self) -> Result<Self::Output> {
        self.endpoint
            .send_dtmf_with_method(&self.session_id, &self.digits, &self.method)
            .map_err(napi_err)
    }
    fn resolve(&mut self, _env: Env, _output: Self::Output) -> Result<Self::JsValue> {
        Ok(())
    }
}

/// Blocking wait for the next endpoint event. Used by `waitForEvent()` —
/// the napi equivalent of Python's `wait_for_event()`. Releases the JS
/// event loop while waiting (runs on napi's thread pool).
pub struct WaitForEventTask {
    rx: crossbeam_channel::Receiver<EndpointEvent>,
    timeout_ms: u64,
}

impl Task for WaitForEventTask {
    type Output = Option<EventInfo>;
    type JsValue = Option<EventInfo>;
    fn compute(&mut self) -> Result<Self::Output> {
        match self.rx.recv_timeout(std::time::Duration::from_millis(self.timeout_ms)) {
            Ok(event) => Ok(Some(event_to_info(&event))),
            Err(_) => Ok(None),
        }
    }
    fn resolve(&mut self, _env: Env, output: Self::Output) -> Result<Self::JsValue> {
        Ok(output)
    }
}

/// AudioFrame compatible with LiveKit's format.
#[napi(object)]
pub struct AudioFrame {
    /// PCM samples as i32 (napi-rs limitation -- i16 not supported).
    /// Converted to/from i16 at the FFI boundary.
    pub data: Vec<i32>,
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
    pub session_id: String,
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

/// Endpoint configuration.
#[napi(object)]
pub struct EndpointConfig {
    pub sip_server: Option<String>,
    pub stun_server: Option<String>,
    pub codecs: Option<Vec<String>>,
    pub log_level: Option<u32>,
    /// Input sample rate in Hz (default: 8000).
    pub input_sample_rate: Option<u32>,
    /// Output sample rate in Hz (default: 8000).
    pub output_sample_rate: Option<u32>,
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
    pub session_id: Option<String>,
    pub session: Option<CallSession>,
    pub error: Option<String>,
    pub reason: Option<String>,
    pub digit: Option<String>,
    pub method: Option<String>,
    pub frequency_hz: Option<f64>,
    pub duration_ms: Option<u32>,
    /// async_id for AudioCaptureComplete / AudioPlayoutComplete /
    /// AudioBufferDrained / AudioCaptureError events. Uses napi's `BigInt`
    /// (NOT `i64`) so JS receives a `bigint`. This MUST match the `bigint`
    /// returned by `sendAudioAsync` / `waitForPlayoutAsync`: the JS broker
    /// keys pending waiters by async-id, and an `i64` here would map to a JS
    /// `number` instead — which never matches a bigint key (`7n !== 7`), so
    /// the waiter hangs forever and TTS audio never flows. A plain `u64`
    /// won't compile either: `#[napi(object)]` needs a bidirectional type and
    /// `u64` implements ToNapiValue but not FromNapiValue.
    pub async_id: Option<BigInt>,
    /// Set on AudioCaptureComplete only: `true` if the completion was
    /// synthesized by `clear_buffer()` (or buffer drop on session
    /// teardown), `false` for a real send completion. LiveKit ignores
    /// this field; pipecat-style adapters that surface a delivered/
    /// dropped boolean from `write_audio_frame` check it.
    pub cancelled: Option<bool>,
}

fn event_to_info(event: &EndpointEvent) -> EventInfo {
    match event {
        EndpointEvent::Registered => EventInfo {
            event_type: "registered".into(),
            session_id: None,
            session: None,
            error: None,
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
            async_id: None,
            cancelled: None,
        },
        EndpointEvent::RegistrationFailed { error } => EventInfo {
            event_type: "registration_failed".into(),
            session_id: None,
            session: None,
            error: Some(error.clone()),
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
            async_id: None,
            cancelled: None,
        },
        EndpointEvent::Unregistered => EventInfo {
            event_type: "unregistered".into(),
            session_id: None,
            session: None,
            error: None,
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
            async_id: None,
            cancelled: None,
        },
        EndpointEvent::CallRinging { session } => EventInfo {
            event_type: "call_ringing".into(),
            session_id: Some(session.session_id.clone()),
            session: Some(session.clone().into()),
            error: None,
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
            async_id: None,
            cancelled: None,
        },
        EndpointEvent::CallStateChanged { session } => EventInfo {
            event_type: "call_state".into(),
            session_id: Some(session.session_id.clone()),
            session: Some(session.clone().into()),
            error: None,
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
            async_id: None,
            cancelled: None,
        },
        EndpointEvent::CallAnswered { session } => EventInfo {
            event_type: "call_answered".into(),
            session_id: Some(session.session_id.clone()),
            session: Some(session.clone().into()),
            error: None,
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
            async_id: None,
            cancelled: None,
        },
        EndpointEvent::CallTerminated { session, reason } => EventInfo {
            event_type: "call_terminated".into(),
            session_id: Some(session.session_id.clone()),
            session: Some(session.clone().into()),
            error: None,
            reason: Some(reason.clone()),
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
            async_id: None,
            cancelled: None,
        },
        EndpointEvent::DtmfReceived {
            call_id,
            digit,
            method,
        } => EventInfo {
            event_type: "dtmf_received".into(),
            session_id: Some(call_id.clone()),
            session: None,
            error: None,
            reason: None,
            digit: Some(digit.to_string()),
            method: Some(method.clone()),
            frequency_hz: None,
            duration_ms: None,
            async_id: None,
            cancelled: None,
        },
        EndpointEvent::BeepDetected {
            call_id,
            frequency_hz,
            duration_ms,
        } => EventInfo {
            event_type: "beep_detected".into(),
            session_id: Some(call_id.clone()),
            session: None,
            error: None,
            reason: None,
            digit: None,
            method: None,
            frequency_hz: Some(*frequency_hz),
            duration_ms: Some(*duration_ms),
            async_id: None,
            cancelled: None,
        },
        EndpointEvent::BeepTimeout { call_id } => EventInfo {
            event_type: "beep_timeout".into(),
            session_id: Some(call_id.clone()),
            session: None,
            error: None,
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
            async_id: None,
            cancelled: None,
        },
        EndpointEvent::Shutdown => EventInfo {
            event_type: "shutdown".into(),
            session_id: None,
            session: None,
            error: None,
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
            async_id: None,
            cancelled: None,
        },
        EndpointEvent::AudioCaptureComplete {
            session_id,
            async_id,
            cancelled,
        } => EventInfo {
            event_type: "audio_capture_complete".into(),
            session_id: Some(session_id.clone()),
            session: None,
            error: None,
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
            async_id: Some(BigInt::from(*async_id)),
            cancelled: Some(*cancelled),
        },
        EndpointEvent::AudioPlayoutComplete {
            session_id,
            async_id,
        } => EventInfo {
            event_type: "audio_playout_complete".into(),
            session_id: Some(session_id.clone()),
            session: None,
            error: None,
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
            async_id: Some(BigInt::from(*async_id)),
            cancelled: None,
        },
        EndpointEvent::AudioBufferDrained {
            session_id,
            async_id,
        } => EventInfo {
            event_type: "audio_buffer_drained".into(),
            session_id: Some(session_id.clone()),
            session: None,
            error: None,
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
            async_id: Some(BigInt::from(*async_id)),
            cancelled: None,
        },
        EndpointEvent::AudioCaptureError {
            session_id,
            async_id,
            error,
        } => EventInfo {
            event_type: "audio_capture_error".into(),
            session_id: Some(session_id.clone()),
            session: None,
            error: Some(error.clone()),
            reason: None,
            digit: None,
            method: None,
            frequency_hz: None,
            duration_ms: None,
            async_id: Some(BigInt::from(*async_id)),
            cancelled: None,
        },
    }
}

/// SIP endpoint — call control and audio I/O.
#[napi]
pub struct SipEndpoint {
    // Arc so that async tasks (e.g., SendDtmfTask) can clone a handle and
    // call methods from napi worker threads without blocking the main
    // Node event loop.
    inner: Arc<RustSipEndpoint>,
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
            input_sample_rate: None,
            output_sample_rate: None,
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
            input_sample_rate: cfg.input_sample_rate.unwrap_or(8000),
            output_sample_rate: cfg.output_sample_rate.unwrap_or(8000),
            audio_processing: agent_transport_core::AudioProcessingConfig {
                jitter_buffer: cfg.jitter_buffer.unwrap_or(false),
                plc: cfg.plc.unwrap_or(false),
                comfort_noise: cfg.comfort_noise.unwrap_or(false),
                ..Default::default()
            },
            ..Default::default()
        };

        let inner = Arc::new(
            RustSipEndpoint::new(rust_config).map_err(napi_err)?,
        );

        Ok(Self { inner })
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

    /// Make an outbound call. Optional `from_uri` sets the SIP From header
    /// (e.g. "sip:+15551234567@provider.com"). If omitted, uses the registered contact URI.
    /// Optional `headers` adds custom SIP headers.
    #[napi]
    pub fn call(
        &self,
        dest_uri: String,
        from_uri: Option<String>,
        headers: Option<HashMap<String, String>>,
        session_id: Option<String>,
    ) -> Result<String> {
        self.inner
            .call_with_from(&dest_uri, from_uri.as_deref(), headers, session_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn answer(&self, session_id: String, code: Option<u32>) -> Result<()> {
        self.inner
            .answer(&session_id, code.unwrap_or(200) as u16)
            .map_err(napi_err)
    }

    #[napi]
    pub fn reject(&self, session_id: String, code: Option<u32>) -> Result<()> {
        self.inner
            .reject(&session_id, code.unwrap_or(486) as u16)
            .map_err(napi_err)
    }

    #[napi]
    pub fn hangup(&self, session_id: String) -> Result<()> {
        self.inner
            .hangup(&session_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn send_dtmf(&self, session_id: String, digits: String, method: Option<String>) -> Result<()> {
        self.inner
            .send_dtmf_with_method(&session_id, &digits, method.as_deref().unwrap_or("rfc2833"))
            .map_err(napi_err)
    }

    /// Non-blocking Promise variant of `sendDtmf`. Runs the RTP pacing on
    /// the napi worker thread pool so the Node main event loop can
    /// continue processing audio/LLM/TTS frames while DTMF digits are
    /// sent on the wire (~280 ms per digit).
    #[napi(ts_return_type = "Promise<void>")]
    pub fn send_dtmf_async(
        &self,
        session_id: String,
        digits: String,
        method: Option<String>,
    ) -> AsyncTask<SendDtmfTask> {
        AsyncTask::new(SendDtmfTask {
            endpoint: Arc::clone(&self.inner),
            session_id,
            digits,
            method: method.unwrap_or_else(|| "rfc2833".to_string()),
        })
    }

    /// Send a SIP INFO message with custom content type and body.
    #[napi]
    pub fn send_info(&self, session_id: String, content_type: String, body: String) -> Result<()> {
        self.inner.send_info(&session_id, &content_type, &body).map_err(napi_err)
    }

    #[napi]
    pub fn transfer(&self, session_id: String, dest_uri: String) -> Result<()> {
        self.inner
            .transfer(&session_id, &dest_uri)
            .map_err(napi_err)
    }

    #[napi]
    pub fn transfer_attended(&self, session_id: String, target_session_id: String) -> Result<()> {
        self.inner
            .transfer_attended(&session_id, &target_session_id)
            .map_err(napi_err)
    }

    /// SIP hold — send Re-INVITE with a=sendonly
    #[napi]
    pub fn hold(&self, session_id: String) -> Result<()> { self.inner.hold(&session_id).map_err(napi_err) }

    /// SIP unhold — send Re-INVITE with a=sendrecv
    #[napi]
    pub fn unhold(&self, session_id: String) -> Result<()> { self.inner.unhold(&session_id).map_err(napi_err) }

    #[napi]
    pub fn mute(&self, session_id: String) -> Result<()> {
        self.inner
            .mute(&session_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn unmute(&self, session_id: String) -> Result<()> {
        self.inner
            .unmute(&session_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn send_audio(&self, session_id: String, frame: AudioFrame) -> Result<()> {
        self.inner
            .send_audio(&session_id, &frame.to_rust())
            .map_err(napi_err)
    }

    #[napi]
    pub fn recv_audio(&self, session_id: String) -> Result<Option<AudioFrame>> {
        self.inner
            .recv_audio(&session_id)
            .map(|opt| opt.map(AudioFrame::from_rust))
            .map_err(napi_err)
    }

    /// Receive audio as raw PCM bytes (little-endian int16). No JS array conversion.
    #[napi]
    pub fn recv_audio_bytes(&self, session_id: String) -> Result<Option<Buffer>> {
        self.inner.recv_audio(&session_id).map(|opt| opt.map(|f| Buffer::from(f.as_bytes()))).map_err(napi_err)
    }

    /// Send raw PCM bytes (little-endian int16) directly.
    #[napi]
    pub fn send_audio_bytes(&self, session_id: String, audio: Buffer, sample_rate: u32, num_channels: u32) -> Result<()> {
        let frame = RustAudioFrame::from_bytes(&audio, sample_rate, num_channels);
        self.inner.send_audio(&session_id, &frame).map_err(napi_err)
    }

    /// Send audio without backpressure — push directly, drop if buffer full.
    #[napi]
    pub fn send_audio_no_backpressure(&self, session_id: String, audio: Buffer, sample_rate: u32, num_channels: u32) -> Result<()> {
        let frame = RustAudioFrame::from_bytes(&audio, sample_rate, num_channels);
        self.inner.send_audio_no_backpressure(&session_id, &frame).map_err(napi_err)
    }

    /// Send background audio to be mixed with agent voice in the RTP send loop.
    #[napi]
    pub fn send_background_audio(&self, session_id: String, audio: Buffer, sample_rate: u32, num_channels: u32) -> Result<()> {
        let f = RustAudioFrame::from_bytes(&audio, sample_rate, num_channels);
        self.inner.send_background_audio(&session_id, &f).map_err(napi_err)
    }

    /// Push audio frame and return the async_id to await on the
    /// endpoint's event channel.
    ///
    /// **Always returns the async_id** — JS MUST always await the
    /// matching `audio_capture_complete` (or `audio_capture_error` on
    /// cancel/flush/drop). Mirrors LiveKit's `capture_audio_frame`
    /// invariant: every request produces exactly one completion event.
    ///
    /// Callers MUST subscribe to the event broker BEFORE calling — the
    /// immediate-emit path can fire before this returns, and an
    /// unsubscribed event would be lost.
    #[napi]
    pub fn send_audio_async(&self, session_id: String, audio: Buffer, sample_rate: u32, num_channels: u32) -> Result<BigInt> {
        let frame = RustAudioFrame::from_bytes(&audio, sample_rate, num_channels);
        self.inner
            .send_audio_async(&session_id, &frame)
            .map(BigInt::from)
            .map_err(napi_err)
    }

    /// Receive audio frame, blocking until available or timeout (ms).
    #[napi]
    pub fn recv_audio_blocking(&self, session_id: String, timeout_ms: Option<u32>) -> Result<Option<AudioFrame>> {
        self.inner
            .recv_audio_blocking(&session_id, timeout_ms.unwrap_or(20) as u64)
            .map(|opt| opt.map(AudioFrame::from_rust))
            .map_err(napi_err)
    }

    /// Receive audio as raw bytes, blocking. Fastest path.
    #[napi]
    pub fn recv_audio_bytes_blocking(&self, session_id: String, timeout_ms: Option<u32>) -> Result<Option<Buffer>> {
        self.inner
            .recv_audio_blocking(&session_id, timeout_ms.unwrap_or(20) as u64)
            .map(|opt| opt.map(|f| Buffer::from(f.as_bytes())))
            .map_err(napi_err)
    }

    /// Receive audio as raw bytes, non-blocking Promise. Runs on libuv thread pool.
    #[napi(ts_return_type = "Promise<Buffer | null>")]
    pub fn recv_audio_bytes_async(&self, session_id: String, timeout_ms: Option<u32>) -> Result<AsyncTask<RecvAudioTask>> {
        let rx = self.inner.incoming_rx(&session_id).map_err(napi_err)?;
        Ok(AsyncTask::new(RecvAudioTask { rx, timeout_ms: timeout_ms.unwrap_or(20) as u64 }))
    }

    /// Wait for playout via Condvar — Promise resolves when buffer empties OR timeout.
    /// **Renamed in 0.2.0** from `waitForPlayoutAsync` (which now returns an
    /// async_id for the new event-based API). Use this when you want a
    /// timeout-bounded "wait for buffer to empty" Promise.
    #[napi(ts_return_type = "Promise<boolean>")]
    pub fn wait_for_playout_blocking(&self, session_id: String, timeout_ms: Option<u32>) -> Result<AsyncTask<SipWaitForPlayoutTask>> {
        let notify = self.inner.playout_notify(&session_id).map_err(napi_err)?;
        Ok(AsyncTask::new(SipWaitForPlayoutTask { notify, timeout_ms: timeout_ms.unwrap_or(5000) as u64 }))
    }

    /// Number of audio frames queued for sending. Multiply by 0.02 for seconds.
    #[napi]
    pub fn queued_frames(&self, session_id: String) -> Result<u32> {
        self.inner.queued_frames(&session_id).map(|n| n as u32).map_err(napi_err)
    }

    /// Queued audio duration in milliseconds (real buffer state).
    #[napi]
    pub fn queued_duration_ms(&self, session_id: String) -> Result<f64> {
        self.inner.queued_duration_ms(&session_id).map_err(napi_err)
    }

    /// Register an async_id for "buffer drained to empty" notification.
    ///
    /// **Always returns the async_id** — JS MUST always await the
    /// matching `audio_playout_complete` (or `audio_capture_error` on
    /// cancel/flush/drop). The completion event always fires
    /// (immediately if buffer already empty, deferred if not).
    /// Multiple concurrent waiters supported. Pause-aware.
    #[napi]
    pub fn wait_for_playout_async(&self, session_id: String) -> Result<BigInt> {
        self.inner
            .wait_for_playout_async(&session_id)
            .map(BigInt::from)
            .map_err(napi_err)
    }

    /// Audio sample rate in Hz.
    #[napi(getter)]
    pub fn input_sample_rate(&self) -> u32 {
        self.inner.input_sample_rate()
    }

    #[napi(getter)]
    pub fn output_sample_rate(&self) -> u32 {
        self.inner.output_sample_rate()
    }

    /// Number of audio channels (always 1 = mono).
    #[napi(getter)]
    pub fn num_channels(&self) -> u32 {
        1
    }

    /// Start recording a call to a stereo WAV file (L=user, R=agent).
    /// Set stereo=false for mono (mixed).
    #[napi]
    pub fn start_recording(&self, session_id: String, path: String, stereo: Option<bool>) -> Result<()> {
        self.inner
            .start_recording(&session_id, &path, stereo.unwrap_or(true))
            .map_err(napi_err)
    }

    #[napi]
    pub fn stop_recording(&self, session_id: String) -> Result<()> {
        self.inner
            .stop_recording(&session_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn detect_beep(
        &self,
        session_id: String,
        timeout_ms: Option<u32>,
        min_duration_ms: Option<u32>,
        max_duration_ms: Option<u32>,
    ) -> Result<()> {
        let config = RustBeepConfig {
            sample_rate: self.inner.input_sample_rate(),
            timeout_ms: timeout_ms.unwrap_or(30000),
            min_duration_ms: min_duration_ms.unwrap_or(80),
            max_duration_ms: max_duration_ms.unwrap_or(5000),
            ..Default::default()
        };
        self.inner
            .detect_beep(&session_id, config)
            .map_err(napi_err)
    }

    #[napi]
    pub fn cancel_beep_detection(&self, session_id: String) -> Result<()> {
        self.inner
            .cancel_beep_detection(&session_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn flush(&self, session_id: String) -> Result<()> {
        self.inner
            .flush(&session_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn clear_buffer(&self, session_id: String) -> Result<()> {
        self.inner
            .clear_buffer(&session_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn wait_for_playout(&self, session_id: String, timeout_ms: Option<u32>) -> Result<bool> {
        self.inner
            .wait_for_playout(&session_id, timeout_ms.unwrap_or(5000) as u64)
            .map_err(napi_err)
    }

    #[napi]
    pub fn pause(&self, session_id: String) -> Result<()> {
        self.inner
            .pause(&session_id)
            .map_err(napi_err)
    }

    #[napi]
    pub fn resume(&self, session_id: String) -> Result<()> {
        self.inner
            .resume(&session_id)
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

    /// Block waiting for the next endpoint event up to `timeout_ms`. Returns
    /// `null` on timeout. Mirrors Python's `wait_for_event()`. Runs on
    /// napi's thread pool so the JS event loop is not blocked.
    #[napi(ts_return_type = "Promise<EventInfo | null>")]
    pub fn wait_for_event(&self, timeout_ms: u32) -> AsyncTask<WaitForEventTask> {
        AsyncTask::new(WaitForEventTask {
            rx: self.inner.events(),
            timeout_ms: timeout_ms as u64,
        })
    }

    /// Shut down the endpoint. Stops event dispatch and tears down SIP stack.
    #[napi]
    pub fn shutdown(&self) -> Result<()> {
        self.inner
            .shutdown()
            .map_err(napi_err)
    }
}

// ─── AudioStreamEndpoint (WebSocket audio streaming) ────────────────────────

#[napi(object)]
pub struct AudioStreamConfigJs {
    pub listen_addr: Option<String>,
    pub plivo_auth_id: Option<String>,
    pub plivo_auth_token: Option<String>,
    pub input_sample_rate: Option<u32>,
    pub output_sample_rate: Option<u32>,
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
        let cfg = config.unwrap_or(AudioStreamConfigJs { listen_addr: None, plivo_auth_id: None, plivo_auth_token: None, input_sample_rate: None, output_sample_rate: None, auto_hangup: None });
        let rc = RustAudioStreamConfig {
            listen_addr: cfg.listen_addr.unwrap_or_else(|| "0.0.0.0:8080".into()),
            input_sample_rate: cfg.input_sample_rate.unwrap_or(8000),
            output_sample_rate: cfg.output_sample_rate.unwrap_or(8000),
            auto_hangup: cfg.auto_hangup.unwrap_or(true),
        };
        let protocol = std::sync::Arc::new(PlivoProtocol::new(
            cfg.plivo_auth_id.unwrap_or_default(),
            cfg.plivo_auth_token.unwrap_or_default(),
        ));
        Ok(Self { inner: RustAudioStreamEndpoint::new(rc, protocol).map_err(napi_err)? })
    }

    #[napi]
    pub fn send_audio(&self, session_id: String, frame: AudioFrame) -> Result<()> {
        self.inner.send_audio(&session_id, &frame.to_rust()).map_err(napi_err)
    }

    #[napi]
    pub fn send_audio_bytes(&self, session_id: String, audio: Buffer, sample_rate: u32, num_channels: u32) -> Result<()> {
        let f = RustAudioFrame::from_bytes(&audio, sample_rate, num_channels);
        self.inner.send_audio(&session_id, &f).map_err(napi_err)
    }

    /// Send background audio to be mixed with agent voice in the send loop.
    #[napi]
    pub fn send_background_audio(&self, session_id: String, audio: Buffer, sample_rate: u32, num_channels: u32) -> Result<()> {
        let f = RustAudioFrame::from_bytes(&audio, sample_rate, num_channels);
        self.inner.send_background_audio(&session_id, &f).map_err(napi_err)
    }

    /// Push audio frame and return the async_id to await on the
    /// endpoint's event channel. See `SipEndpoint::sendAudioAsync` for
    /// the full LiveKit-faithful contract — every request produces
    /// exactly one completion event; callers MUST subscribe before
    /// calling.
    #[napi]
    pub fn send_audio_async(&self, session_id: String, audio: Buffer, sample_rate: u32, num_channels: u32) -> Result<BigInt> {
        let frame = RustAudioFrame::from_bytes(&audio, sample_rate, num_channels);
        self.inner
            .send_audio_async(&session_id, &frame)
            .map(BigInt::from)
            .map_err(napi_err)
    }

    #[napi]
    pub fn recv_audio(&self, session_id: String) -> Result<Option<AudioFrame>> {
        self.inner.recv_audio(&session_id).map(|o| o.map(AudioFrame::from_rust)).map_err(napi_err)
    }

    #[napi]
    pub fn recv_audio_bytes(&self, session_id: String) -> Result<Option<Buffer>> {
        self.inner.recv_audio(&session_id).map(|o| o.map(|f| Buffer::from(f.as_bytes()))).map_err(napi_err)
    }

    #[napi]
    pub fn recv_audio_blocking(&self, session_id: String, timeout_ms: Option<u32>) -> Result<Option<AudioFrame>> {
        self.inner.recv_audio_blocking(&session_id, timeout_ms.unwrap_or(20) as u64).map(|o| o.map(AudioFrame::from_rust)).map_err(napi_err)
    }

    #[napi]
    pub fn recv_audio_bytes_blocking(&self, session_id: String, timeout_ms: Option<u32>) -> Result<Option<Buffer>> {
        self.inner.recv_audio_blocking(&session_id, timeout_ms.unwrap_or(20) as u64).map(|o| o.map(|f| Buffer::from(f.as_bytes()))).map_err(napi_err)
    }

    /// Receive audio as raw bytes, non-blocking Promise. Runs on libuv thread pool.
    #[napi(ts_return_type = "Promise<Buffer | null>")]
    pub fn recv_audio_bytes_async(&self, session_id: String, timeout_ms: Option<u32>) -> Result<AsyncTask<RecvAudioTask>> {
        let rx = self.inner.incoming_rx(&session_id).map_err(napi_err)?;
        Ok(AsyncTask::new(RecvAudioTask { rx, timeout_ms: timeout_ms.unwrap_or(20) as u64 }))
    }

    /// Wait for playout via Plivo checkpoint Condvar — Promise resolves when
    /// the server confirms playedStream OR timeout. **Renamed in 0.2.0** from
    /// `waitForPlayoutAsync`; the new event-based `waitForPlayoutAsync` returns
    /// a BigInt async_id instead.
    #[napi(ts_return_type = "Promise<boolean>")]
    pub fn wait_for_playout_blocking(&self, session_id: String, timeout_ms: Option<u32>) -> Result<AsyncTask<WaitForPlayoutTask>> {
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
    pub fn queued_duration_ms(&self, session_id: String) -> Result<f64> {
        self.inner.queued_duration_ms(&session_id).map_err(napi_err)
    }

    /// Register an async_id for "buffer drained to empty" notification.
    /// See `SipEndpoint::waitForPlayoutAsync` for the LiveKit-faithful
    /// contract — always returns the async_id; the completion event
    /// always fires.
    #[napi]
    pub fn wait_for_playout_async(&self, session_id: String) -> Result<BigInt> {
        self.inner
            .wait_for_playout_async(&session_id)
            .map(BigInt::from)
            .map_err(napi_err)
    }

    #[napi]
    pub fn hangup(&self, session_id: String, auth_id: Option<String>, auth_token: Option<String>) -> Result<()> {
        self.inner.hangup_with_auth(&session_id, auth_id.as_deref(), auth_token.as_deref()).map_err(napi_err)
    }

    #[napi]
    pub fn send_raw_message(&self, session_id: String, message: String) -> Result<()> {
        self.inner.send_raw_message(&session_id, &message).map_err(napi_err)
    }

    #[napi]
    pub fn detect_beep(
        &self,
        session_id: String,
        timeout_ms: Option<u32>,
        min_duration_ms: Option<u32>,
        max_duration_ms: Option<u32>,
    ) -> Result<()> {
        let config = RustBeepConfig {
            sample_rate: self.inner.input_sample_rate(),
            timeout_ms: timeout_ms.unwrap_or(30000),
            min_duration_ms: min_duration_ms.unwrap_or(80),
            max_duration_ms: max_duration_ms.unwrap_or(5000),
            ..Default::default()
        };
        self.inner.detect_beep(&session_id, config).map_err(napi_err)
    }

    #[napi]
    pub fn cancel_beep_detection(&self, session_id: String) -> Result<()> {
        self.inner.cancel_beep_detection(&session_id).map_err(napi_err)
    }

    #[napi]
    pub fn start_recording(&self, session_id: String, path: String, stereo: Option<bool>) -> Result<()> {
        self.inner.start_recording(&session_id, &path, stereo.unwrap_or(true)).map_err(napi_err)
    }

    #[napi]
    pub fn stop_recording(&self, session_id: String) -> Result<()> {
        self.inner.stop_recording(&session_id).map_err(napi_err)
    }

    #[napi]
    pub fn poll_event(&self) -> Result<Option<EventInfo>> {
        match self.inner.events().try_recv() {
            Ok(event) => Ok(Some(event_to_info(&event))),
            Err(_) => Ok(None),
        }
    }

    /// Block waiting for the next event up to `timeout_ms`. Returns null
    /// on timeout. Mirrors Python's `wait_for_event()`.
    #[napi(ts_return_type = "Promise<EventInfo | null>")]
    pub fn wait_for_event(&self, timeout_ms: u32) -> AsyncTask<WaitForEventTask> {
        AsyncTask::new(WaitForEventTask {
            rx: self.inner.events(),
            timeout_ms: timeout_ms as u64,
        })
    }

    #[napi(getter)]
    pub fn input_sample_rate(&self) -> u32 { self.inner.input_sample_rate() }

    #[napi(getter)]
    pub fn output_sample_rate(&self) -> u32 { self.inner.output_sample_rate() }

    #[napi(getter)]
    pub fn num_channels(&self) -> u32 { 1 }

    #[napi]
    pub fn shutdown(&self) -> Result<()> { self.inner.shutdown().map_err(napi_err) }
}

// ─── Logging ────────────────────────────────────────────────────────────────

/// Initialize Rust tracing with the given log level filter.
///
/// Call this once, before creating any endpoints, to see Rust-level logs
/// (SIP signaling, RTP pacing, audio buffer state, etc.) on stderr. Without
/// this, `RUST_LOG` has no effect from Node because no tracing subscriber
/// is installed by default.
///
/// Examples:
///   initLogging("debug")                                      # agent-transport debug, rsipstack core info
///   initLogging("trace")                                      # everything including rsipstack
///   initLogging("agent_transport=debug,rsipstack=debug")      # both at debug
///   initLogging("info")                                       # default
///
/// `RUST_LOG` env var overrides the `filter` argument when set.
///
/// Calling more than once returns an error (tracing can only be
/// initialized once per process).
#[napi(js_name = "initLogging")]
pub fn init_logging(filter: Option<String>) -> Result<()> {
    use tracing_subscriber::EnvFilter;
    let filter = filter.unwrap_or_else(|| "info".to_string());
    let raw = std::env::var("RUST_LOG").unwrap_or(filter);
    // Expand shorthand levels to filtered versions that skip DNS/transport noise,
    // matching the Python binding's init_logging behavior so logs look the same
    // across both bindings.
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
        .map_err(|e| Error::from_reason(format!("tracing already initialized: {}", e)))
}
