# Agent Transport

Pure Rust multi-transport library for AI voice agents with Python and TypeScript bindings.

## Transports

- **SIP** (`sip/`): Direct SIP calling via rsipstack + RTP. Register, make/receive calls, send/receive audio.
- **Audio Streaming** (`audio_stream/`): Plivo WebSocket audio streaming. Receives mu-law audio over WebSocket JSON messages. Feature: `audio-stream`.

Both produce/consume the same `AudioFrame` format: int16 PCM, 16kHz mono.

## Project Structure

```
crates/
├── agent-transport/          # Rust core: SIP + audio streaming transports
│   └── src/
│       ├── sip/              # SIP transport (rsipstack + rtp)
│       └── audio_stream/     # Plivo WebSocket audio streaming
├── agent-transport-python/   # PyO3 bindings
├── agent-transport-node/     # napi-rs bindings
└── beep-detector/            # Standalone beep detection

python/                       # Python adapters
├── agent_transport/
│   ├── sip/
│   │   ├── pipecat/          # Pipecat SipTransport adapter
│   │   └── livekit/          # LiveKit AgentServer + SIP I/O adapters
│   └── audio_stream/
│       ├── pipecat/          # Pipecat AudioStreamTransport + AudioStreamServer
│       └── livekit/          # (planned) LiveKit audio stream adapters

node/                         # Node.js adapter types
```

## Build

No system dependencies — pure Rust.

```bash
cargo build                                     # Core library
cargo build --features audio-stream             # + Plivo audio streaming
cargo build --features audio-processing         # + jitter buffer, PLC, comfort noise
cd crates/agent-transport-python && maturin develop  # Python binding
cd crates/agent-transport-node && npm run build      # Node binding
cd python && pip install -e ".[all]"                 # Python adapters
```

## Key Design Principles

- **Audio pacing in Rust, not Python**: RTP send loop uses `tokio::time::interval` for precise 20ms pacing.
  Python adapters use `recv_audio_blocking()` via `run_in_executor` — no `asyncio.sleep` polling loops.
  This avoids jitter at high concurrency.
- **Feature-gated optional modules**: jitter-buffer, plc, comfort-noise, audio-stream
- **Backward-compatible exports**: SipEndpoint re-exported at crate root for bindings

## Git Conventions

- Do NOT include "Co-Authored-By" lines or any mention of Claude/Anthropic in commit messages.

## Testing

```bash
cargo test                                          # Unit tests
cargo test --features audio-processing              # With audio processing
cargo test --features audio-stream                  # With audio streaming
SIP_USERNAME=xxx SIP_PASSWORD=yyy \
  cargo test -p agent-transport --features integration  # Live SIP tests
```
