# Agent Transport

Pure Rust SIP transport for AI agents with Python and TypeScript bindings.
See SPEC.md for full specification.

## Project Structure

Cargo workspace with four crates:

- `crates/beep-detector/` — Standalone voicemail beep detector (Goertzel filters)
- `crates/agent-transport/` — SIP endpoint: rsipstack (signaling) + rtc-rtp (media)
- `crates/agent-transport-python/` — PyO3 bindings (maturin build)
- `crates/agent-transport-node/` — napi-rs bindings (npm package)

## Build

No system dependencies required — pure Rust.

```bash
cargo build
```

### Python binding

```bash
cd crates/agent-transport-python
maturin develop  # dev install
maturin build    # build wheel
```

### Node.js binding

```bash
cd crates/agent-transport-node
npm install
npm run build
```

## Key Conventions

- Pure Rust: rsipstack for SIP signaling, rtc-rtp for RTP packet framing
- G.711 PCMU/PCMA codec with 8kHz <-> 16kHz resampling
- Audio frames are int16 PCM, 16kHz mono (matching LiveKit AudioFrame format)
- Events dispatched through crossbeam channels
- Sync public API wrapping async tokio internals
- DTMF: RFC 4733 (default) or SIP INFO (fallback)
- STUN binding for NAT traversal (default: stun-fb.plivo.com:3478)

## Testing

```bash
cargo test                                          # Unit tests (~38 tests)
cargo test -p beep-detector                         # Beep detector tests independently
SIP_USERNAME=xxx SIP_PASSWORD=yyy \
  cargo test -p agent-transport --features integration  # Live SIP tests
```

## Architecture Notes

- rsipstack handles SIP registration, INVITE/BYE, REFER, re-INVITE, digest auth
- RTP audio sent/received over plain UDP (one socket per call)
- G.711 encode/decode implemented as ITU-T lookup tables
- SDP offer/answer constructed and parsed in sdp.rs
- No global state — all state owned by SipEndpoint struct
- Tokio runtime created internally, public API is synchronous
