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

### Observability adapters

The Python LiveKit adapters carry a thin observability layer:

- `agent_transport/sip/livekit/observability.py` (and the `audio_stream/livekit` mirror) — builds a `voice.SessionReport` from the JobContext, attaches transport tags via the SDK `Tagger`, and delegates the upload to LiveKit's telemetry helpers. Uploads land at `agent-observability` (recordings + OTLP). Auth: Bearer JWT signed with `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`.
- `agent_transport/sip/livekit/judging.py` — runs LiveKit's `JudgeGroup` post-session and uploads outcomes via the SDK telemetry channel. Hook fires once per call from both SIP and audio_stream cleanup paths. Before judges run it tags the session with `evaluations:enabled` so the obs dashboard surfaces its Evaluation button immediately, even if judging fails or is still pending. Callers can also flag a session with `metadata={"evaluations": True}` (handled in `_ensure_transport_tags`) for custom eval pipelines that bypass `JudgeGroup`.

Session IDs in `room.name` / `room.sid` use the bare transport ID (`ws-<hex>` for audio_stream, SIP Call-ID for SIP) — no `transport-` prefix — so they match the Node adapter's room IDs and the obs dashboard groups them consistently.

Node side is intentionally lighter: `crates/agent-transport-node/adapters/livekit/observability.ts` covers the SessionReport / OTLP construction (since the Node SDK has no `Tagger`), but there's no `JudgeGroup` integration — the Node SDK doesn't expose one. Do not port the Python `judging.py` to Node without first adding upstream support. Two contracts to preserve when editing the Node file: (a) the recording multipart must upload **before** OTLP records — the obs server's `mergeSessionRawReport` UPDATEs an existing row and silently no-ops if it doesn't exist yet; (b) events embedded in the session-report record are normalized to snake_case keys + float-second timestamps to match the Python SDK's Pydantic projection that the obs UI is keyed off.

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
