/// Audio codec selection.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Codec {
    Opus,
    PCMU,
    PCMA,
    G722,
}

impl Codec {
    /// Codec identifier string for the SIP stack.
    pub fn pjsua_name(&self) -> &'static str {
        match self {
            Codec::Opus => "opus/48000/2",
            Codec::PCMU => "PCMU/8000/1",
            Codec::PCMA => "PCMA/8000/1",
            Codec::G722 => "G722/16000/1",
        }
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
    /// SIP registrar/proxy server hostname (e.g., "sip.plivo.com")
    pub sip_server: String,

    /// SIP server port (default: 5060 for UDP)
    pub sip_port: u16,

    /// STUN server address (e.g., "stun.plivo.com:3478")
    pub stun_server: String,

    /// Optional TURN server for relay NAT traversal
    pub turn_server: Option<TurnConfig>,

    /// Preferred codecs in priority order
    pub codecs: Vec<Codec>,

    /// SIP stack log level (0 = none, 6 = verbose)
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
}

impl Default for EndpointConfig {
    fn default() -> Self {
        Self {
            sip_server: "phone.plivo.com".into(),
            sip_port: 5060,
            stun_server: "stun.plivo.com:3478".into(),
            turn_server: None,
            codecs: vec![Codec::PCMU, Codec::PCMA],
            log_level: 3,
            user_agent: "plivo-agent-endpoint/0.1.0".into(),
            local_port: 0,
            enable_ice: true,
            enable_srtp: false,
            register_expires: 120,
        }
    }
}
