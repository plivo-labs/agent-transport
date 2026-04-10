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
├── agent-transport-python/   # PyO3 bindings + Python adapters
│   ├── src/                  # Rust PyO3 bindings
│   └── adapters/
│       └── agent_transport/  # Python adapters (SIP + audio stream)
│           ├── sip/
│           │   ├── livekit/  # LiveKit AgentServer + SIP I/O adapters
│           │   └── pipecat/  # Pipecat SipTransport adapter
│           └── audio_stream/
│               ├── livekit/  # LiveKit AudioStreamServer + audio stream I/O adapters
│               └── pipecat/  # Pipecat AudioStreamTransport + AudioStreamServer
├── agent-transport-node/     # napi-rs bindings + Node adapters
│   ├── src/                  # Rust napi-rs bindings
│   └── adapters/
│       └── livekit/          # TypeScript LiveKit adapters (SIP + AudioStream)
└── beep-detector/            # Standalone beep detection
```

## Build

System dependencies: `cmake` (for audiopus_sys/aws-lc-sys).

Requires: Rust, C compiler, CMake. On CMake 4.x, set `CMAKE_POLICY_VERSION_MINIMUM=3.5`.

```bash
cargo build                                     # Core library
cargo build --features audio-stream             # + Plivo audio streaming
cargo build --features audio-processing         # + jitter buffer, PLC, comfort noise
cd crates/agent-transport-python && pip install -e ".[all]"  # Python binding + adapters
cd crates/agent-transport-node && npm install && npm run build:all  # Node binding + adapters
```

## Key Design Principles

- **Audio pacing in Rust, not Python**: RTP send loop uses `tokio::time::interval` for precise 20ms pacing.
  Python adapters use `recv_audio_blocking()` via `run_in_executor` — no `asyncio.sleep` polling loops.
  This avoids jitter at high concurrency.
- **Feature-gated optional modules**: jitter-buffer, plc, comfort-noise, audio-stream
- **Backward-compatible exports**: SipEndpoint re-exported at crate root for bindings

## Releasing

Publish is triggered by PR labels — no tags or GitHub Releases needed.

1. Bump the version in the relevant file:
   - **Python**: `crates/agent-transport-python/pyproject.toml`
   - **Node**: `crates/agent-transport-node/package.json` + all `crates/agent-transport-node/npm/*/package.json`
2. **Version bumps must be in a dedicated PR** — do not mix version bumps with feature changes. Both Python and Node can be bumped together in the same PR, but the PR should contain only version changes.
3. Add labels to the PR:
   - **Release trigger** (required to publish):
     - `release-python-sdk` — publishes to PyPI
     - `release-node-sdk` — publishes to npm
   - Release PRs should only have release trigger labels. Do **not** apply feature labels (`python`, `node`, `core`) to version bump PRs.
   - **Release notes labels** (for feature/fix PRs only):
     - `python` — include this PR in Python release notes
     - `node` — include this PR in Node release notes
     - `core` — include this PR in both Python and Node release notes
4. Merge the PR to `main`. The build workflow runs, then the publish workflow picks up artifacts and publishes.

Python and Node releases are independent — you can release one without the other.

### Prerequisites (one-time setup)

- **PyPI**: Configure trusted publisher at pypi.org for `agent-transport` (workflow: `publish-python.yml`, environment: `pypi`)
- **npm**: Create automation token, store as `NPM_TOKEN` in GitHub Environment `npm`
- **GitHub**: Create environments `pypi` and `npm`, create labels `release-python-sdk` and `release-node-sdk`

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
