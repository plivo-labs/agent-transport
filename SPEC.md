# Agent Transport Specification

## Overview

A pure Rust multi-transport library for AI voice agents. Supports:
- **SIP transport**: Direct SIP calling via rsipstack + RTP
- **Audio streaming transport**: Plivo WebSocket audio streaming

Exposes bindings to Python (PyO3) and TypeScript/Node.js (napi-rs), with
adapters for LiveKit Agents and Pipecat frameworks.

Audio frame format: int16 PCM, 16kHz mono — compatible with both LiveKit
(configurable, Deepgram STT defaults to 16kHz) and Pipecat (16kHz default).

## Architecture

```
┌─────────────────────┐  ┌──────────────────────┐
│  Python (PyO3)      │  │  TypeScript (napi-rs) │
│  pip install ...    │  │  npm install ...      │
└────────┬────────────┘  └────────┬──────────────┘
         └───────┐    ┌──────────┘
                 ▼    ▼
    ┌──────────────────────────────┐
    │      Rust Core (lib)         │
    │                              │
    │  SipEndpoint implementation  │
    │  AudioFrame encode/decode    │
    │  Event dispatch (channels)   │
    │  Call state machine          │
    │  DTMF / REFER / Mute        │
    ├──────────────────────────────┤
    │  rsipstack (SIP stack)       │
    │  RTP over UDP                │
    │  STUN + rport NAT traversal  │
    │  PCMU + PCMA codecs (G.711) │
    └──────────────────────────────┘
         │              │
    SIP over UDP    RTP over UDP
         │              │
         ▼              ▼
    SIP Provider    Media (audio)
```

## Dependencies

No system dependencies — pure Rust.

### Rust Dependencies

| Crate | Purpose |
|-------|---------|
| `rsipstack` | SIP stack (registration, call control, dialog management) |
| `rtc-rtp` | RTP packet parsing |
| `audio-codec-algorithms` | G.711 mu-law/A-law encode/decode |
| `tokio` | Async runtime for event dispatch |
| `crossbeam-channel` | Lock-free audio frame passing |
| `thiserror` | Error types |
| `tracing` | Structured logging |
| `pyo3` | Python bindings (agent-transport-python crate) |
| `napi` / `napi-derive` | Node.js bindings (agent-transport-node crate) |

## Core Types

### AudioFrame

```rust
pub struct AudioFrame {
    /// PCM samples, interleaved by channel, signed 16-bit
    pub data: Vec<i16>,
    /// Sample rate in Hz (8000 for G.711, 16000 after resampling)
    pub sample_rate: u32,
    /// Number of audio channels (1 = mono, 2 = stereo)
    pub num_channels: u32,
    /// Samples per channel in this frame
    pub samples_per_channel: u32,
}
```

Matches LiveKit's `rtc.AudioFrame` format for drop-in compatibility.

### CallSession

```rust
pub struct CallSession {
    pub call_uuid: String,
    pub call_id: String,         // Internal call ID
    pub direction: CallDirection, // Inbound | Outbound
    pub state: CallState,
    pub remote_uri: String,
    pub local_uri: String,
    pub extra_headers: HashMap<String, String>,
}

pub enum CallDirection {
    Inbound,
    Outbound,
}

pub enum CallState {
    Calling,
    Incoming,
    Early,       // 180 Ringing / 183 Session Progress
    Connecting,
    Confirmed,   // Media flowing
    Disconnected,
    Failed(String),
}
```

### EndpointEvent

```rust
pub enum EndpointEvent {
    // Registration
    Registered,
    RegistrationFailed { error: String },
    Unregistered,

    // Call lifecycle
    IncomingCall { session: CallSession },
    CallStateChanged { session: CallSession },
    CallMediaActive { call_id: String },
    CallTerminated { session: CallSession, reason: String },

    // DTMF
    DtmfReceived { call_id: String, digit: char, method: String },

    // Beep detection
    BeepDetected { call_id: String, frequency_hz: f64, duration_ms: u32 },
    BeepTimeout { call_id: String },
}
```

## Public API

### EndpointConfig

```rust
pub struct EndpointConfig {
    pub sip_server: String,        // e.g., "phone.plivo.com"
    pub sip_port: u16,             // default: 5060
    pub stun_server: String,       // e.g., "stun-fb.plivo.com:3478"
    pub codecs: Vec<Codec>,        // [Codec::PCMU, Codec::PCMA]
    pub log_level: u32,            // SIP stack log level (0-6)
    pub user_agent: String,        // e.g., "agent-transport/0.1.0"
    pub local_port: u16,           // Local SIP port (0 = auto)
    pub register_expires: u32,     // Registration expiry in seconds
    pub audio_processing: AudioProcessingConfig,
}

pub enum Codec {
    PCMU,
    PCMA,
}
```

### SipEndpoint

```rust
impl SipEndpoint {
    /// Create and initialize a new SIP endpoint
    pub fn new(config: EndpointConfig) -> Result<Self>;

    /// Register with SIP server using digest authentication
    pub fn register(&self, username: &str, password: &str) -> Result<()>;

    /// Unregister from SIP server
    pub fn unregister(&self) -> Result<()>;

    /// Check registration status
    pub fn is_registered(&self) -> bool;

    /// Make an outbound call
    pub fn call(
        &self,
        dest_uri: &str,
        headers: Option<HashMap<String, String>>,
    ) -> Result<String>; // returns call_id

    /// Answer an incoming call
    pub fn answer(&self, call_id: &str, code: u16) -> Result<()>; // 200 = accept

    /// Reject/decline an incoming call
    pub fn reject(&self, call_id: &str, code: u16) -> Result<()>; // 486, 603, etc.

    /// Hang up an active call
    pub fn hangup(&self, call_id: &str) -> Result<()>;

    /// Send DTMF digits (RFC 2833)
    pub fn send_dtmf(&self, call_id: &str, digits: &str) -> Result<()>;

    /// Transfer call via SIP REFER
    pub fn transfer(&self, call_id: &str, dest_uri: &str) -> Result<()>;

    /// Attended transfer (two calls)
    pub fn transfer_attended(
        &self,
        call_id: &str,
        target_call_id: &str,
    ) -> Result<()>;

    /// Mute/unmute outgoing audio
    pub fn mute(&self, call_id: &str) -> Result<()>;
    pub fn unmute(&self, call_id: &str) -> Result<()>;

    /// SIP hold/unhold via Re-INVITE
    pub fn hold(&self, call_id: &str) -> Result<()>;
    pub fn unhold(&self, call_id: &str) -> Result<()>;

    /// Send an audio frame into the call
    pub fn send_audio(&self, call_id: &str, frame: &AudioFrame) -> Result<()>;

    /// Receive the next audio frame from the call
    /// Returns None if no frame is available (non-blocking)
    pub fn recv_audio(&self, call_id: &str) -> Result<Option<AudioFrame>>;

    /// Receive audio frame, blocking until available or timeout
    pub fn recv_audio_blocking(&self, call_id: &str, timeout_ms: u64) -> Result<Option<AudioFrame>>;

    /// Playback control
    pub fn flush(&self, call_id: &str) -> Result<()>;
    pub fn clear_buffer(&self, call_id: &str) -> Result<()>;
    pub fn wait_for_playout(&self, call_id: &str, timeout_ms: u64) -> Result<bool>;
    pub fn pause(&self, call_id: &str) -> Result<()>;
    pub fn resume(&self, call_id: &str) -> Result<()>;
    pub fn queued_frames(&self, call_id: &str) -> Result<usize>;

    /// Get the event receiver channel
    pub fn events(&self) -> crossbeam_channel::Receiver<EndpointEvent>;

    /// Shut down the endpoint
    pub fn shutdown(&self) -> Result<()>;
}
```

### AudioStreamEndpoint

```rust
impl AudioStreamEndpoint {
    /// Create a new audio streaming endpoint
    pub fn new(config: AudioStreamConfig) -> Result<Self>;

    /// Send an audio frame to a session
    pub fn send_audio(&self, session_id: &str, frame: &AudioFrame) -> Result<()>;

    /// Send audio with completion callback (for backpressure)
    pub fn send_audio_with_callback(&self, session_id: &str, frame: &AudioFrame, on_complete: CompletionCallback) -> Result<()>;

    /// Receive the next audio frame (non-blocking)
    pub fn recv_audio(&self, session_id: &str) -> Result<Option<AudioFrame>>;

    /// Receive audio frame, blocking until available or timeout
    pub fn recv_audio_blocking(&self, session_id: &str, timeout_ms: u64) -> Result<Option<AudioFrame>>;

    /// Playback control
    pub fn flush(&self, session_id: &str) -> Result<()>;
    pub fn clear_buffer(&self, session_id: &str) -> Result<()>;
    pub fn checkpoint(&self, session_id: &str, name: Option<&str>) -> Result<String>;
    pub fn wait_for_playout(&self, session_id: &str, timeout_ms: u64) -> Result<bool>;
    pub fn pause(&self, session_id: &str) -> Result<()>;
    pub fn resume(&self, session_id: &str) -> Result<()>;
    pub fn queued_frames(&self, session_id: &str) -> Result<usize>;

    /// Mute/unmute
    pub fn mute(&self, session_id: &str) -> Result<()>;
    pub fn unmute(&self, session_id: &str) -> Result<()>;

    /// Send DTMF digits
    pub fn send_dtmf(&self, session_id: &str, digits: &str) -> Result<()>;

    /// Send arbitrary WebSocket message
    pub fn send_raw_message(&self, session_id: &str, message: &str) -> Result<()>;

    /// OGG/Opus recording
    pub fn start_recording(&self, session_id: &str, path: &str, stereo: bool) -> Result<()>;
    pub fn stop_recording(&self, session_id: &str) -> Result<()>;

    /// Hang up a session
    pub fn hangup(&self, session_id: &str) -> Result<()>;

    /// Get the event receiver channel
    pub fn events(&self) -> Receiver<EndpointEvent>;

    /// Get the configured sample rate
    pub fn sample_rate(&self) -> u32;

    /// Shut down the endpoint
    pub fn shutdown(&self) -> Result<()>;
}
```

## NAT Traversal

- **STUN**: Binding request (RFC 5389) to discover public IP. Configured via `stun_server` in EndpointConfig.
- **rport**: Enabled by default (RFC 3581). Uses rport/received from SIP responses for NAT-discovered address.

## DTMF

Uses RFC 2833 (RTP telephone-event) for DTMF send/receive.

## SIP REFER (Transfer)

- Blind transfer: transfer the call to a destination URI
- Attended transfer: connect two active calls

## Python Binding API (PyO3)

```python
from agent_transport import SipEndpoint, AudioFrame, CallSession

# Create endpoint
ep = SipEndpoint(
    sip_server="phone.plivo.com",
    stun_server="stun-fb.plivo.com:3478",
    codecs=["pcmu", "pcma"],
)

# Register
ep.register("username", "password")

# Event handling (callback-based)
@ep.on("incoming_call")
def on_incoming(event):
    ep.answer(event["session"].call_id)

# Outbound call
call_id = ep.call("sip:+15551234567@phone.plivo.com")

# Send audio
frame = AudioFrame(data=pcm_samples, sample_rate=16000, num_channels=1)
ep.send_audio(call_id, frame)

# DTMF
ep.send_dtmf(call_id, "1234#")

# Transfer
ep.transfer(call_id, "sip:+15559876543@phone.plivo.com")

# Hangup
ep.hangup(call_id)
ep.shutdown()
```

## TypeScript Binding API (napi-rs)

```typescript
import { SipEndpoint, AudioFrame } from 'agent-transport';

const ep = new SipEndpoint({
  sipServer: 'phone.plivo.com',
  stunServer: 'stun-fb.plivo.com:3478',
  codecs: ['pcmu', 'pcma'],
});

ep.register('username', 'password');

ep.on('incoming_call', (event) => {
  ep.answer(event.callId);
});

const callId = ep.call('sip:+15551234567@phone.plivo.com');
ep.sendDtmf(callId, '1234#');
ep.transfer(callId, 'sip:+15559876543@phone.plivo.com');
ep.hangup(callId);
ep.shutdown();
```

## LiveKit Agents Compatibility

The `AudioFrame` format matches LiveKit's `rtc.AudioFrame`:

| Field | Our Type | LiveKit Type | Match |
|-------|----------|-------------|-------|
| data | `Vec<i16>` | `memoryview` (int16) | Yes |
| sample_rate | `u32` (16000) | `int` (16000) | Yes |
| num_channels | `u32` (1) | `int` (1) | Yes |
| samples_per_channel | `u32` (320) | `int` (320) | Yes |

LiveKit Agents adapters are provided for both SIP and Audio Streaming:
- `AgentServer` (SIP) and `AudioStreamServer` wrap the Rust endpoints
- `JobContext` with `ctx.session = session` wires audio I/O automatically
- `TransportRoom` facade provides `room.on("sip_dtmf_received")`, `local_participant.publish_dtmf()`, background audio mixing
- `JobProcess` + `ctx.proc.userdata` matches LiveKit's prewarm pattern
- OGG/Opus recording via Rust transport layer

## Testing Strategy

1. **Unit tests**: Rust core logic (call state machine, audio frame conversion, codecs, DTMF)
2. **Integration tests**: Register with SIP provider, make call to echo service
3. **Cross-platform CI**: GitHub Actions matrix (macOS, Ubuntu)
4. **Binding tests**: Python and Node.js calling the native module

## File Structure

```
agent_transport/
├── SPEC.md                           # This file
├── CLAUDE.md                         # Build conventions for Claude
├── Cargo.toml                        # Workspace root
│
├── crates/
│   ├── agent-transport/              # Rust core
│   │   ├── Cargo.toml
│   │   └── src/
│   │       ├── lib.rs                # Public API re-exports
│   │       ├── sip/
│   │       │   ├── endpoint.rs       # SipEndpoint implementation
│   │       │   ├── sdp.rs            # SDP offer/answer + STUN binding
│   │       │   ├── rtp_transport.rs  # RTP send/recv over UDP
│   │       │   ├── dtmf.rs           # RFC 2833 DTMF encode/decode
│   │       │   ├── rtcp.rs           # RTCP sender/receiver reports
│   │       │   └── call.rs           # CallSession, CallState
│   │       ├── audio_stream/         # Plivo WebSocket audio streaming
│   │       ├── config.rs             # EndpointConfig, Codec
│   │       ├── audio.rs              # AudioFrame
│   │       ├── events.rs             # EndpointEvent
│   │       ├── error.rs              # Error types
│   │       └── recorder.rs           # OGG/Opus recording
│   │
│   ├── agent-transport-python/       # Python bindings (PyO3)
│   │   ├── Cargo.toml
│   │   └── src/lib.rs
│   │
│   ├── agent-transport-node/         # Node.js bindings (napi-rs)
│   │   ├── Cargo.toml
│   │   ├── package.json
│   │   └── src/lib.rs
│   │
│   └── beep-detector/                # Standalone beep/AMD detection
│
├── python/                           # Python adapters
│   └── agent_transport/
│       ├── sip/
│       │   ├── pipecat/              # Pipecat SipTransport adapter
│       │   └── livekit/              # LiveKit AgentServer + SIP I/O adapters
│       └── audio_stream/
│           ├── pipecat/              # Pipecat AudioStreamTransport + AudioStreamServer
│           └── livekit/              # LiveKit AudioStreamServer + audio stream I/O adapters
│
├── node/
│   └── agent-transport-livekit/  # TypeScript LiveKit adapters (SIP + AudioStream)
│
└── examples/
    ├── livekit/
    │   ├── sip_agent.py              # SIP voice agent (Python)
    │   ├── sip_agent.ts              # SIP voice agent (TypeScript)
    │   ├── sip_multi_agent.py        # Multi-agent handoff (Python)
    │   ├── sip_multi_agent.ts        # Multi-agent handoff (TypeScript)
    │   ├── audio_stream_agent.py     # Audio streaming agent (Python)
    │   ├── audio_stream_agent.ts     # Audio streaming agent (TypeScript)
    │   ├── audio_stream_multi_agent.py   # Audio streaming multi-agent (Python)
    │   └── audio_stream_multi_agent.ts   # Audio streaming multi-agent (TypeScript)
    ├── pipecat/
    │   ├── sip_agent.py
    │   └── audio_stream_agent.py
    └── cli/
        └── phone.py
```
