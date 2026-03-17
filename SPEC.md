# Agent Endpoint Specification

## Overview

A Rust library providing a SIP user agent that can register with Plivo, make/receive calls,
send/receive audio frames, send DTMF, and perform call transfers (SIP REFER). Exposes
bindings to both Python (PyO3) and TypeScript/Node.js (napi-rs).

The audio frame interface is designed to be compatible with LiveKit Agents' `AudioFrame`
format (int16 PCM, 48kHz), enabling drop-in replacement of LiveKit's WebRTC transport
with direct Plivo SIP connectivity.

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
    │  pjsua-sys (bindgen FFI)     │
    │  Safe wrappers around C API   │
    ├──────────────────────────────┤
    │  libpjproject (C, static)    │
    │  SIP / Media engine          │
    │  SIP UDP + RTP + ICE/STUN   │
    │  Opus + PCMU + PCMA codecs  │
    └──────────────────────────────┘
         │              │
    SIP over UDP    RTP over UDP
         │              │
         ▼              ▼
    Plivo SIP       Media (audio)
    Endpoint
```

## Target Platforms

| Platform | Architecture | Build System |
|----------|-------------|-------------|
| macOS | x86_64, aarch64 (Apple Silicon) | autotools (./configure + make) |
| Linux (Debian/Ubuntu) | x86_64, aarch64 | autotools |
| Linux (CentOS/RHEL) | x86_64, aarch64 | autotools |
| Windows | x86_64 | MSVC (Visual Studio) |

## Dependencies

### SIP Library Build Dependencies

| Platform | Packages |
|----------|----------|
| macOS | `brew install opus openssl pkg-config` |
| Debian/Ubuntu | `build-essential libasound2-dev libssl-dev libopus-dev pkg-config` |
| CentOS/RHEL | `Development Tools`, `openssl-devel alsa-lib-devel opus-devel` |
| Windows | Visual Studio 2019+, OpenSSL SDK, Opus SDK |

### Rust Dependencies

| Crate | Purpose |
|-------|---------|
| `bindgen` | Generate Rust FFI from pjsua.h |
| `cc` | Compile C shim if needed |
| `tokio` | Async runtime for event dispatch |
| `crossbeam-channel` | Lock-free audio frame passing |
| `thiserror` | Error types |
| `tracing` | Structured logging |
| `pyo3` | Python bindings (optional feature) |
| `napi` / `napi-derive` | Node.js bindings (optional feature) |

## Core Types

### AudioFrame

```rust
pub struct AudioFrame {
    /// PCM samples, interleaved by channel, signed 16-bit
    pub data: Vec<i16>,
    /// Sample rate in Hz (48000 for Opus, 8000 for PCMU/PCMA)
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
    pub call_id: i32,            // Internal call ID
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
    CallState { session: CallSession },
    CallMediaActive { call_id: i32 },
    CallTerminated { session: CallSession, reason: String },

    // Media
    AudioReceived { call_id: i32, frame: AudioFrame },

    // DTMF
    DtmfReceived { call_id: i32, digit: char },
}
```

## Public API

### EndpointConfig

```rust
pub struct EndpointConfig {
    pub sip_server: String,        // e.g., "sip.plivo.com"
    pub sip_port: u16,             // default: 5060
    pub stun_server: String,       // e.g., "stun.plivo.com:3478"
    pub turn_server: Option<TurnConfig>,
    pub codecs: Vec<Codec>,        // [Codec::Opus, Codec::PCMU]
    pub log_level: u32,            // SIP stack log level (0-6)
    pub user_agent: String,        // e.g., "plivo-agent-endpoint/0.1.0"
}

pub enum Codec {
    Opus,
    PCMU,
    PCMA,
    G722,
}

pub struct TurnConfig {
    pub server: String,
    pub username: String,
    pub password: String,
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
    ) -> Result<i32>; // returns call_id

    /// Answer an incoming call
    pub fn answer(&self, call_id: i32, code: u16) -> Result<()>; // 200 = accept

    /// Reject/decline an incoming call
    pub fn reject(&self, call_id: i32, code: u16) -> Result<()>; // 486, 603, etc.

    /// Hang up an active call
    pub fn hangup(&self, call_id: i32) -> Result<()>;

    /// Send DTMF digits (RFC 2833)
    pub fn send_dtmf(&self, call_id: i32, digits: &str) -> Result<()>;

    /// Transfer call via SIP REFER
    pub fn transfer(&self, call_id: i32, dest_uri: &str) -> Result<()>;

    /// Attended transfer (two calls)
    pub fn transfer_attended(
        &self,
        call_id: i32,
        target_call_id: i32,
    ) -> Result<()>;

    /// Mute outgoing audio
    pub fn mute(&self, call_id: i32) -> Result<()>;

    /// Unmute outgoing audio
    pub fn unmute(&self, call_id: i32) -> Result<()>;

    /// Send an audio frame into the call
    pub fn send_audio(&self, call_id: i32, frame: &AudioFrame) -> Result<()>;

    /// Receive the next audio frame from the call
    /// Returns None if no frame is available (non-blocking)
    pub fn recv_audio(&self, call_id: i32) -> Result<Option<AudioFrame>>;

    /// Get the event receiver channel
    pub fn events(&self) -> crossbeam_channel::Receiver<EndpointEvent>;

    /// Shut down the endpoint
    pub fn shutdown(&self) -> Result<()>;
}
```

## SIP Stack Integration Details

### Initialization Sequence

1. Create the SIP stack instance
2. Configure logging, media, and transport settings
3. Initialize with config
4. Create UDP transport
5. Start the SIP stack
6. Add SIP account with credentials

### Audio Bridge

The SIP stack has a conference bridge that connects audio ports. For programmatic audio:

1. Create a custom audio media port that:
   - Captures audio from the conference bridge -> `recv_audio()` frames
   - Plays audio from `send_audio()` frames -> into conference bridge
2. Register this port with the conference bridge
3. On call connect, bridge the call's audio to our custom port

This avoids needing a physical sound device — audio is purely in-memory.

### Callback Mapping

| SIP Callback | Our Event |
|----------------|-----------|
| `on_reg_state2()` | `Registered` / `RegistrationFailed` / `Unregistered` |
| `on_incoming_call()` | `IncomingCall` |
| `on_call_state()` | `CallState` / `CallTerminated` |
| `on_call_media_state()` | `CallMediaActive` |
| `on_dtmf_digit()` | `DtmfReceived` |
| `on_call_transfer_status()` | (transfer progress) |

### Audio Port Implementation

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────┐
│  Remote SIP      │     │  Conference      │     │  Our custom  │
│  endpoint        │◄───▶│  Bridge          │◄───▶│  media port  │
│  (Plivo)         │ RTP │                  │     │              │
└──────────────────┘     └──────────────────┘     │  get_frame() │
                                                   │  -> recv_audio│
                                                   │              │
                                                   │  put_frame() │
                                                   │  <- send_audio│
                                                   └──────────────┘
```

### NAT Traversal

The SIP stack handles NAT traversal natively:
- **STUN**: Configure via endpoint config
- **TURN**: Configure via account's TURN config
- **ICE**: Enable via media config
- **rport**: Enabled by default (RFC 3581)

### Codec Priority

Set via codec priority configuration after init:
- Opus: priority 255 (highest)
- PCMU: priority 254
- PCMA: priority 253
- Disable all others: priority 0

### DTMF

Use RFC 2833 (RTP event) for DTMF.
Receive via the DTMF callback.

### SIP REFER (Transfer)

- Blind transfer: transfer the call to a destination URI
- Attended transfer: connect two active calls

## Python Binding API (PyO3)

```python
from plivo_endpoint import PlivoEndpoint, AudioFrame, CallSession

# Create endpoint
ep = PlivoEndpoint(
    sip_server="sip.plivo.com",
    stun_server="stun.plivo.com:3478",
    codecs=["opus", "pcmu"],
)

# Register
ep.register("username", "password")

# Event handling (callback-based)
@ep.on("incoming_call")
def on_incoming(call: CallSession):
    call.answer()

@ep.on("audio")
def on_audio(call_id: int, frame: AudioFrame):
    # frame.data: bytes (int16 PCM)
    # frame.sample_rate: int
    # frame.num_channels: int
    pass

# Outbound call
call_id = ep.call("sip:+15551234567@sip.plivo.com")

# Send audio
frame = AudioFrame(data=pcm_bytes, sample_rate=48000, num_channels=1)
ep.send_audio(call_id, frame)

# DTMF
ep.send_dtmf(call_id, "1234#")

# Transfer
ep.transfer(call_id, "sip:+15559876543@sip.plivo.com")

# Hangup
ep.hangup(call_id)
ep.shutdown()
```

## TypeScript Binding API (napi-rs)

```typescript
import { PlivoEndpoint, AudioFrame, CallSession } from 'plivo-endpoint';

const ep = new PlivoEndpoint({
  sipServer: 'sip.plivo.com',
  stunServer: 'stun.plivo.com:3478',
  codecs: ['opus', 'pcmu'],
});

await ep.register('username', 'password');

ep.on('incomingCall', async (call: CallSession) => {
  await call.answer();
});

ep.on('audio', (callId: number, frame: AudioFrame) => {
  // frame.data: Int16Array
  // frame.sampleRate: number
  // frame.numChannels: number
});

const callId = await ep.call('sip:+15551234567@sip.plivo.com');
ep.sendDtmf(callId, '1234#');
ep.transfer(callId, 'sip:+15559876543@sip.plivo.com');
ep.hangup(callId);
ep.shutdown();
```

## LiveKit Agents Compatibility

The `AudioFrame` format matches LiveKit's `rtc.AudioFrame`:

| Field | Our Type | LiveKit Type | Match |
|-------|----------|-------------|-------|
| data | `Vec<i16>` | `memoryview` (int16) | Yes |
| sample_rate | `u32` (48000) | `int` (48000) | Yes |
| num_channels | `u32` (1) | `int` (1) | Yes |
| samples_per_channel | `u32` (960) | `int` (960) | Yes |

To use with LiveKit agents, replace `RoomIO` with a `SipIO` adapter that:
1. Wraps `SipEndpoint` as the transport
2. Implements `AudioInput` (async iterator yielding our `AudioFrame`)
3. Implements `AudioOutput` (`push(frame)` calls `send_audio()`)
4. Maps `IncomingCall` / `CallTerminated` to participant lifecycle events

## SIP Library Dependency

Uses prebuilt system packages via pkg-config. No compilation from source required.

### Install per platform

| Platform | Command |
|----------|---------|
| macOS | `brew install pjproject pkg-config` |
| Debian/Ubuntu | `sudo apt install libpjproject-dev pkg-config clang` |
| CentOS/RHEL | `sudo dnf install epel-release && sudo dnf install pjproject-devel pkg-config clang` |
| Alpine | `apk add pjproject-dev pkgconf clang` |

### How it works

The `pjsua-sys/build.rs` calls `pkg-config --cflags --libs libpjproject` to discover:
- Include paths for headers (bindgen uses these)
- Library paths and names (cargo links against these)
- Platform-specific defines (e.g., `-DPJ_AUTOCONF=1`)

Additional system libraries (OpenSSL, CoreAudio frameworks, ALSA) are linked
explicitly per platform in build.rs.

## Testing Strategy

1. **Unit tests**: Rust core logic (call state machine, audio frame conversion)
2. **Integration tests**: Register with Plivo test account, make call to echo service
3. **Cross-platform CI**: GitHub Actions matrix (macOS, Ubuntu, Windows)
4. **Binding tests**: Python pytest + Node.js jest calling the native module

## File Structure

```
agent_endpoint/
├── SPEC.md                           # This file
├── CLAUDE.md                         # Build conventions for Claude
├── Cargo.toml                        # Workspace root
│
├── crates/
│   ├── pjsua-sys/                    # Raw FFI bindings (bindgen)
│   │   ├── Cargo.toml
│   │   ├── build.rs                  # pkg-config + bindgen
│   │   ├── wrapper.h                 # #include <pjsua-lib/pjsua.h>
│   │   └── src/lib.rs                # bindgen output re-export
│   │
│   ├── agent-endpoint/               # Safe Rust API
│   │   ├── Cargo.toml
│   │   └── src/
│   │       ├── lib.rs                # Public API re-exports
│   │       ├── endpoint.rs           # SipEndpoint implementation
│   │       ├── config.rs             # EndpointConfig
│   │       ├── call.rs               # CallSession, call control
│   │       ├── audio.rs              # AudioFrame, media port bridge
│   │       ├── dtmf.rs               # DTMF send/receive
│   │       ├── events.rs             # EndpointEvent, dispatcher
│   │       └── error.rs              # Error types
│   │
│   ├── agent-endpoint-python/        # Python bindings
│   │   ├── Cargo.toml
│   │   ├── pyproject.toml
│   │   └── src/lib.rs
│   │
│   └── agent-endpoint-node/          # Node.js bindings
│       ├── Cargo.toml
│       ├── package.json
│       └── src/lib.rs
│
└── examples/
    ├── register.rs
    ├── outbound_call.rs
    ├── inbound_echo.rs
    └── dtmf_menu.rs
```
