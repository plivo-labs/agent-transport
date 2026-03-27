# Agent Transport

Transport library (SIP/RTP & Audio Streaming) for voice AI agents to be used with frameworks like [LiveKit Agents](https://github.com/livekit/agents/) and [Pipecat](https://github.com/pipecat-ai/pipecat). 

Agent Transport provides signaling and media primitives that AI agent frameworks need to make and receive voice calls. The core is written in Rust for efficient, low-jitter packet processing — audio pacing, RTP handling, and jitter buffering. Framework adapters for LiveKit Agents and Pipecat are provided as drop-in plugins. Bindings in Python and TypeScript/Node.js are also available for other use cases.

## Transports

**SIP/RTP** — Register with any SIP provider, make and receive calls over RTP. G.711 codecs (PCMU/PCMA), DTMF (RFC 2833), NAT traversal (STUN, rport), hold/unhold, call transfer. No server required, directly connect with telephony providers over SIP like [Plivo](https://plivo.com) .

**Audio Streaming** — Websocket based audio streaming that works with cloud telephony providers like Plivo that support bidirectional audio streaming.

Both transports produce and consume the same `AudioFrame` format (int16 PCM, 16kHz mono), so agent code works identically regardless of transport.

## Framework Adapters

### LiveKit Agents

Same `AgentSession` pipeline — replace `AgentServer` + `room=ctx.room` with `AgentServer` + `ctx.start()`:

```python
# LiveKit WebRTC                              # Agent Transport SIP/RTP
from livekit.agents import AgentServer        from agent_transport.sip.livekit import AgentServer
server = AgentServer()                         server = AgentServer(sip_username=..., sip_password=...)

@server.rtc_session()                          @server.sip_session()
async def entrypoint(ctx):                     async def entrypoint(ctx):
    session = AgentSession(...)                    session = AgentSession(...)
    await session.start(                           await ctx.start(session, agent=Assistant())
        agent=Assistant(), room=ctx.room)

cli.run_app(server)                            server.run()
```

Full examples: [`sip_agent.py`](examples/livekit/sip_agent.py) · [`sip_multi_agent.py`](examples/livekit/sip_multi_agent.py)

See [LiveKit SIP Transport docs](docs/livekit_interface_sip.md) for recording, Prometheus metrics, outbound API, and full reference.

### Pipecat

Drop-in replacements for `WebsocketServerTransport`:

```python
# SIP/RTP
transport = SipTransport(ep, call_id, params=TransportParams(...))

# Plivo Audio Streaming
transport = AudioStreamTransport(ep, session_id, params=TransportParams(...))
```

## Getting Started

### Prerequisites

- **Python 3.9+**
- **Rust** — install via [rustup](https://rustup.rs/): `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`
- **maturin** — Python ↔ Rust build tool: `pip install maturin`
- A **SIP provider** account (e.g., [Plivo](https://plivo.com), [Telnyx](https://telnyx.com), [Twilio](https://twilio.com)) with SIP endpoint credentials and a phone number
- **Deepgram** API key for speech-to-text — [deepgram.com](https://deepgram.com)
- **OpenAI** API key for LLM and text-to-speech — [platform.openai.com](https://platform.openai.com)

### Step 1: Create a Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### Step 2: Build the Rust Python Binding

```bash
cd crates/agent-transport-python && maturin develop
cd ../..
```

### Step 3: Install Python Adapters

```bash
cd python && pip install -e ".[all]"    # LiveKit + Pipecat adapters
cd ..
```

Or install only the adapter you need:

```bash
pip install -e ".[livekit]"   # LiveKit adapter only
pip install -e ".[pipecat]"   # Pipecat adapter only
```

### Step 4: Install AI Provider Packages

The adapters install the core frameworks but not the individual AI service plugins. Install them:

**For LiveKit examples:**

```bash
pip install python-dotenv \
    livekit-plugins-deepgram livekit-plugins-openai \
    livekit-plugins-silero livekit-plugins-turn-detector
```

**For Pipecat examples:**

```bash
pip install python-dotenv "pipecat-ai[deepgram,openai]"
```

### Step 5: Download Model Files

The turn detector plugin (used in LiveKit examples) requires an ONNX model from Hugging Face. This is a one-time download:

```bash
pip install transformers huggingface-hub
python -c "from livekit.plugins.turn_detector.multilingual import _EUORunnerMultilingual; _EUORunnerMultilingual._download_files()"
```

### Step 6: Set Environment Variables

```bash
export SIP_USERNAME=<your SIP username>
export SIP_PASSWORD=<your SIP password>
export SIP_DOMAIN=phone.plivo.com
export DEEPGRAM_API_KEY=<your Deepgram key>
export OPENAI_API_KEY=<your OpenAI key>
```

### Step 7: Run an Example

```bash
python examples/livekit/sip_agent.py dev
```

CLI modes: `start` (production, INFO logging), `dev` (development, DEBUG for adapters), `debug` (full debug including Rust SIP/RTP).

### Building the Rust Core (Optional)

Only needed if you are developing the Rust core itself:

```bash
cargo build                                     # Core library (SIP transport)
cargo build --features audio-stream             # + Plivo audio streaming
cargo build --features audio-processing         # + jitter buffer, PLC, comfort noise
```

### Node.js Bindings (Optional)

```bash
cd crates/agent-transport-node && npm run build
```

## Examples

| Example | Description |
|---------|-------------|
| [`livekit/sip_agent.py`](examples/livekit/sip_agent.py) | SIP voice agent with tool calling, turn detection, preemptive generation |
| [`livekit/sip_multi_agent.py`](examples/livekit/sip_multi_agent.py) | Multi-agent with greeter → sales/support handoff and tool calling |
| [`livekit/audio_stream_agent.py`](examples/livekit/audio_stream_agent.py) | LiveKit agent over Plivo audio streaming |
| [`pipecat/sip_agent.py`](examples/pipecat/sip_agent.py) | Pipecat pipeline over SIP/RTP |
| [`pipecat/audio_stream_agent.py`](examples/pipecat/audio_stream_agent.py) | Pipecat pipeline over Plivo audio streaming |
| [`cli/phone.py`](examples/cli/phone.py) | Interactive CLI softphone with mic/speaker, DTMF, mute, hold/unhold |

See also: [Feature Flags & CLI Phone docs](docs/features.md)

## License

MIT
