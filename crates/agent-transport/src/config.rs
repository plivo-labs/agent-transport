/// Audio codec selection.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Codec {
    Opus,
    PCMU,
    PCMA,
    G722,
}

impl Codec {
    /// RTP payload type number (RFC 3551).
    pub fn payload_type(&self) -> u8 {
        match self {
            Codec::PCMU => 0,
            Codec::PCMA => 8,
            Codec::G722 => 9,
            Codec::Opus => 111, // dynamic
        }
    }

    /// SDP rtpmap encoding name (e.g., "PCMU/8000").
    pub fn rtpmap_line(&self) -> &'static str {
        match self {
            Codec::PCMU => "PCMU/8000",
            Codec::PCMA => "PCMA/8000",
            Codec::G722 => "G722/16000",
            Codec::Opus => "opus/48000/2",
        }
    }

    /// Native sample rate in Hz.
    pub fn sample_rate(&self) -> u32 {
        match self {
            Codec::PCMU | Codec::PCMA => 8000,
            Codec::G722 => 16000,
            Codec::Opus => 48000,
        }
    }

    /// Encode PCM samples to G.711 bytes.
    pub fn encode(&self, samples: &[i16]) -> Vec<u8> {
        match self {
            Codec::PCMU => samples.iter().map(|&s| audio_codec_algorithms::encode_ulaw(s)).collect(),
            Codec::PCMA => samples.iter().map(|&s| audio_codec_algorithms::encode_alaw(s)).collect(),
            _ => samples.iter().map(|&s| audio_codec_algorithms::encode_ulaw(s)).collect(),
        }
    }

    /// Decode G.711 bytes to PCM samples.
    pub fn decode(&self, bytes: &[u8]) -> Vec<i16> {
        match self {
            Codec::PCMU => bytes.iter().map(|&b| audio_codec_algorithms::decode_ulaw(b)).collect(),
            Codec::PCMA => bytes.iter().map(|&b| audio_codec_algorithms::decode_alaw(b)).collect(),
            _ => bytes.iter().map(|&b| audio_codec_algorithms::decode_ulaw(b)).collect(),
        }
    }

    /// Silence byte for this codec.
    pub fn silence_byte(&self) -> u8 {
        match self { Codec::PCMU => 0xFF, Codec::PCMA => 0xD5, _ => 0xFF }
    }
}

/// TURN server configuration.
#[derive(Debug, Clone)]
pub struct TurnConfig {
    pub server: String,
    pub username: String,
    pub password: String,
}

/// Configuration for the SIP endpoint.
#[derive(Debug, Clone)]
pub struct EndpointConfig {
    /// SIP registrar/proxy server hostname (e.g., "phone.plivo.com")
    pub sip_server: String,

    /// SIP server port (default: 5060 for UDP)
    pub sip_port: u16,

    /// STUN server address for public IP discovery
    pub stun_server: String,

    /// Optional TURN server for relay NAT traversal
    pub turn_server: Option<TurnConfig>,

    /// Preferred codecs in priority order
    pub codecs: Vec<Codec>,

    /// Log level (0 = none, 6 = verbose)
    pub log_level: u32,

    /// User-Agent header string
    pub user_agent: String,

    /// Local SIP port to bind (0 = auto)
    pub local_port: u16,

    /// Enable ICE for media NAT traversal
    pub enable_ice: bool,

    /// Enable SRTP for media encryption
    pub enable_srtp: bool,

    /// Registration expiry in seconds
    pub register_expires: u32,

    /// Optional audio processing settings.
    /// Requires corresponding Cargo features: `jitter-buffer`, `plc`, `comfort-noise`.
    pub audio_processing: AudioProcessingConfig,
}

/// Configuration for optional audio processing features.
///
/// All features require corresponding Cargo feature flags to be enabled.
/// If a feature is requested but not compiled in, it is silently ignored.
#[derive(Debug, Clone)]
pub struct AudioProcessingConfig {
    /// Enable adaptive jitter buffer (requires `jitter-buffer` feature).
    pub jitter_buffer: bool,

    /// Enable packet loss concealment using G.711 Appendix I pitch-based
    /// concealment (requires `plc` feature).
    pub plc: bool,

    /// Enable comfort noise generation during silence to keep NAT pinholes
    /// open and prevent carrier timeouts (requires `comfort-noise` feature).
    pub comfort_noise: bool,

    /// Comfort noise level in dBov (0 = loud, 127 = silence). Default: 60.
    pub comfort_noise_level: u8,
}

impl Default for AudioProcessingConfig {
    fn default() -> Self {
        Self {
            jitter_buffer: false,
            plc: false,
            comfort_noise: false,
            comfort_noise_level: 60,
        }
    }
}

impl Default for EndpointConfig {
    fn default() -> Self {
        Self {
            sip_server: "phone.plivo.com".into(),
            sip_port: 5060,
            stun_server: "stun-fb.plivo.com:3478".into(),
            turn_server: None,
            codecs: vec![Codec::PCMU, Codec::PCMA],
            log_level: 3,
            user_agent: "agent-transport/0.1.0".into(),
            local_port: 0,
            enable_ice: false,
            enable_srtp: false,
            register_expires: 120,
            audio_processing: AudioProcessingConfig::default(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_config() {
        let c = EndpointConfig::default();
        assert_eq!(c.sip_server, "phone.plivo.com");
        assert_eq!(c.sip_port, 5060);
        assert_eq!(c.stun_server, "stun-fb.plivo.com:3478");
        assert_eq!(c.codecs, vec![Codec::PCMU, Codec::PCMA]);
        assert_eq!(c.register_expires, 120);
        assert!(c.turn_server.is_none());
    }

    #[test]
    fn test_codec_rtp_properties() {
        assert_eq!(Codec::PCMU.payload_type(), 0);
        assert_eq!(Codec::PCMA.payload_type(), 8);
        assert_eq!(Codec::PCMU.rtpmap_line(), "PCMU/8000");
        assert_eq!(Codec::PCMA.rtpmap_line(), "PCMA/8000");
        assert_eq!(Codec::PCMU.sample_rate(), 8000);
        assert_eq!(Codec::Opus.sample_rate(), 48000);
    }
}
