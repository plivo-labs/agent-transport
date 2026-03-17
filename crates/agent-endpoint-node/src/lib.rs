use napi::bindgen_prelude::*;
use napi_derive::napi;

use agent_endpoint::{
    AudioFrame as RustAudioFrame, BeepDetectorConfig as RustBeepConfig,
    CallSession as RustCallSession, Codec as RustCodec,
    EndpointConfig as RustEndpointConfig, SipEndpoint as RustSipEndpoint,
};

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
pub struct CallSession {
    pub call_id: i32,
    pub call_uuid: Option<String>,
    pub direction: String,
    pub state: String,
    pub remote_uri: String,
    pub local_uri: String,
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

/// The main Plivo SIP endpoint.
#[napi]
pub struct PlivoEndpoint {
    inner: RustSipEndpoint,
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
            .unwrap_or_else(|| vec!["opus".into(), "pcmu".into()])
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
            sip_server: cfg.sip_server.unwrap_or_else(|| "sip.plivo.com".into()),
            stun_server: cfg
                .stun_server
                .unwrap_or_else(|| "stun.plivo.com:3478".into()),
            codecs: codec_list,
            log_level: cfg.log_level.unwrap_or(3),
            ..Default::default()
        };

        let inner = RustSipEndpoint::new(rust_config)
            .map_err(|e| Error::from_reason(e.to_string()))?;

        Ok(Self { inner })
    }

    #[napi]
    pub fn register(&self, username: String, password: String) -> Result<()> {
        self.inner
            .register(&username, &password)
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn unregister(&self) -> Result<()> {
        self.inner
            .unregister()
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn is_registered(&self) -> bool {
        self.inner.is_registered()
    }

    #[napi]
    pub fn call(&self, dest_uri: String) -> Result<i32> {
        self.inner
            .call(&dest_uri, None)
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn answer(&self, call_id: i32, code: Option<u32>) -> Result<()> {
        self.inner
            .answer(call_id, code.unwrap_or(200) as u16)
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn reject(&self, call_id: i32, code: Option<u32>) -> Result<()> {
        self.inner
            .reject(call_id, code.unwrap_or(486) as u16)
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn hangup(&self, call_id: i32) -> Result<()> {
        self.inner
            .hangup(call_id)
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn send_dtmf(&self, call_id: i32, digits: String) -> Result<()> {
        self.inner
            .send_dtmf(call_id, &digits)
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn transfer(&self, call_id: i32, dest_uri: String) -> Result<()> {
        self.inner
            .transfer(call_id, &dest_uri)
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn transfer_attended(&self, call_id: i32, target_call_id: i32) -> Result<()> {
        self.inner
            .transfer_attended(call_id, target_call_id)
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn mute(&self, call_id: i32) -> Result<()> {
        self.inner
            .mute(call_id)
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn unmute(&self, call_id: i32) -> Result<()> {
        self.inner
            .unmute(call_id)
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn send_audio(&self, call_id: i32, frame: AudioFrame) -> Result<()> {
        self.inner
            .send_audio(call_id, &frame.to_rust())
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn recv_audio(&self, call_id: i32) -> Result<Option<AudioFrame>> {
        self.inner
            .recv_audio(call_id)
            .map(|opt| opt.map(AudioFrame::from_rust))
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn start_recording(&self, call_id: i32, path: String) -> Result<()> {
        self.inner
            .start_recording(call_id, &path)
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn stop_recording(&self, call_id: i32) -> Result<()> {
        self.inner
            .stop_recording(call_id)
            .map_err(|e| Error::from_reason(e.to_string()))
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
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn cancel_beep_detection(&self, call_id: i32) -> Result<()> {
        self.inner
            .cancel_beep_detection(call_id)
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn flush(&self, call_id: i32) -> Result<()> {
        self.inner
            .flush(call_id)
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn clear_buffer(&self, call_id: i32) -> Result<()> {
        self.inner
            .clear_buffer(call_id)
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn wait_for_playout(&self, call_id: i32, timeout_ms: Option<u32>) -> Result<bool> {
        self.inner
            .wait_for_playout(call_id, timeout_ms.unwrap_or(5000) as u64)
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn pause(&self, call_id: i32) -> Result<()> {
        self.inner
            .pause(call_id)
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn resume(&self, call_id: i32) -> Result<()> {
        self.inner
            .resume(call_id)
            .map_err(|e| Error::from_reason(e.to_string()))
    }

    #[napi]
    pub fn poll_event(&self) -> Result<Option<String>> {
        match self.inner.events().try_recv() {
            Ok(event) => Ok(Some(format!("{:?}", event))),
            Err(_) => Ok(None),
        }
    }

    #[napi]
    pub fn shutdown(&self) -> Result<()> {
        self.inner
            .shutdown()
            .map_err(|e| Error::from_reason(e.to_string()))
    }
}
