# Agent Transport

Transport library (SIP/RTP & Audio Streaming) for voice AI agents to be used with frameworks like [LiveKit Agents](https://github.com/livekit/agents/) and [Pipecat](https://github.com/pipecat-ai/pipecat). 

Agent Transport provides signaling and media primitives that AI agent frameworks need to make and receive voice calls. The core is written in Rust for efficient, low-jitter packet processing — audio pacing, RTP handling, and jitter buffering. Framework adapters for LiveKit Agents and Pipecat are provided as drop-in plugins. Bindings in Python and TypeScript/Node.js are also available for other use cases.

## Transports

**SIP/RTP** — Register with any SIP provider, make and receive calls over RTP. G.711 codecs (PCMU/PCMA), DTMF (RFC 2833), NAT traversal (STUN, rport), hold/unhold, call transfer. No media server required.

**Audio Streaming** — WebSocket server for cloud telephony providers like [Plivo](https://plivo.com) that support bidirectional audio streaming.

Both transports produce and consume the same `AudioFrame` format (int16 PCM, 16kHz mono), so agent code works identically regardless of transport.

## Framework Adapters

### LiveKit Agents

Drop-in replacements for `RoomAudioInput` / `RoomAudioOutput` — no LiveKit server or WebRTC needed:

```python
# SIP/RTP
session.input.audio = SipAudioInput(ep, call_id)
session.output.audio = SipAudioOutput(ep, call_id)

# Plivo Audio Streaming
session.input.audio = AudioStreamInput(ep, session_id)
session.output.audio = AudioStreamOutput(ep, session_id)
```

### Pipecat

Drop-in replacements for `WebsocketServerTransport`:

```python
# SIP/RTP
transport = SipTransport(ep, call_id, params=TransportParams(...))

# Plivo Audio Streaming
transport = AudioStreamTransport(ep, session_id, params=TransportParams(...))
```

## Installation

### Rust Core

No system dependencies — pure Rust.

```bash
cargo build                                     # Core library (SIP transport)
cargo build --features audio-stream             # + Plivo audio streaming
cargo build --features audio-processing         # + jitter buffer, PLC, comfort noise
```

### Python Bindings + Adapters

```bash
cd crates/agent-transport-python && maturin develop   # Native binding
cd python && pip install -e ".[all]"                   # LiveKit + Pipecat adapters
```

Install only the adapter you need:

```bash
pip install -e ".[livekit]"   # LiveKit adapter only
pip install -e ".[pipecat]"   # Pipecat adapter only
```

### Node.js Bindings

```bash
cd crates/agent-transport-node && npm run build
```

## Examples

### Python

| Example | Description |
|---------|-------------|
| [`livekit_sip_agent.py`](examples/livekit_sip_agent.py) | LiveKit VoicePipelineAgent over SIP/RTP (Deepgram STT, GPT-4 mini, OpenAI TTS) |
| [`livekit_audio_stream_agent.py`](examples/livekit_audio_stream_agent.py) | LiveKit VoicePipelineAgent over Plivo audio streaming |
| [`pipecat_sip_agent.py`](examples/pipecat_sip_agent.py) | Pipecat pipeline over SIP/RTP (Deepgram STT, OpenAI LLM + TTS) |
| [`pipecat_audio_stream_agent.py`](examples/pipecat_audio_stream_agent.py) | Pipecat pipeline over Plivo audio streaming |
| [`cli_phone.py`](examples/cli_phone.py) | Interactive CLI softphone with mic/speaker, DTMF, mute, hold/unhold |

#### Running the CLI Phone (Python)

```bash
pip install sounddevice numpy

# Outbound call
SIP_USERNAME=xxx SIP_PASSWORD=yyy \
    python examples/cli_phone.py sip:+15551234567@phone.plivo.com

# Inbound (wait for a call)
SIP_USERNAME=xxx SIP_PASSWORD=yyy python examples/cli_phone.py
```

### Node.js

| Example | Description |
|---------|-------------|
| [`cli_phone.js`](examples/cli_phone.js) | SIP CLI demonstrating signaling, DTMF, pause/resume, flush/clear, wait-for-playout |

#### Running the CLI Phone (Node.js)

```bash
cd crates/agent-transport-node && npm run build

# Outbound call
SIP_USERNAME=xxx SIP_PASSWORD=yyy \
    node examples/cli_phone.js sip:+15551234567@phone.plivo.com
```

## Feature Flags

| Feature | Description |
|---------|-------------|
| `audio-stream` | WebSocket audio streaming transport |
| `jitter-buffer` | RTP jitter buffer (requires neteq) |
| `plc` | Packet loss concealment |
| `comfort-noise` | Comfort noise generation |
| `audio-processing` | All three above combined |
| `integration` | Live SIP integration tests (requires credentials) |



## License

MIT
