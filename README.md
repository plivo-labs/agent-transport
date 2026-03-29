# Agent Transport

Transport library (SIP/RTP & Audio Streaming) for voice AI agents to be used with frameworks like [LiveKit Agents](https://github.com/livekit/agents/) and [Pipecat](https://github.com/pipecat-ai/pipecat). 

Agent Transport provides signaling and media primitives that AI agent frameworks need to make and receive voice calls. The core is written in Rust for efficient, low-jitter packet processing — audio pacing, RTP handling, and jitter buffering. Framework adapters for LiveKit Agents and Pipecat are provided as drop-in plugins. Bindings in Python and TypeScript/Node.js are also available for other use cases.

## Transports

**SIP/RTP** — Register with any SIP provider, make and receive calls over RTP. G.711 codecs (PCMU/PCMA), DTMF (RFC 2833), NAT traversal (STUN, rport), hold/unhold, call transfer. No server required, directly connect with telephony providers over SIP like [Plivo](https://plivo.com) .

**Audio Streaming** — Websocket based audio streaming that works with cloud telephony providers like Plivo that support bidirectional audio streaming.

Both transports produce and consume the same `AudioFrame` format (int16 PCM, 16kHz mono), so agent code works identically regardless of transport.

## Framework Adapters

### LiveKit Agents

Same `AgentSession` pipeline — add `ctx.session = session` to wire SIP/audio stream transport:

```python
# LiveKit WebRTC                                # Agent Transport SIP/RTP
from livekit.agents import AgentServer          from agent_transport.sip.livekit import AgentServer
server = AgentServer()                          server = AgentServer(sip_username=..., sip_password=...)

@server.rtc_session()                           @server.sip_session()
async def entrypoint(ctx):                      async def entrypoint(ctx):
    session = AgentSession(...)                     session = AgentSession(...)
    await session.start(                            ctx.session = session  # wires SIP audio I/O
        agent=Assistant(),                          await session.start(
        room=ctx.room)                                  agent=Assistant(),
                                                        room=ctx.room)
cli.run_app(server)                             server.run()
```

Full examples: [`sip_agent.py`](examples/livekit/sip_agent.py) · [`sip_multi_agent.py`](examples/livekit/sip_multi_agent.py) · [`audio_stream_agent.py`](examples/livekit/audio_stream_agent.py)

See [LiveKit SIP Transport docs](docs/livekit_interface_sip.md) for recording, Prometheus metrics, outbound API, and full reference.

### Pipecat

Replaces `FastAPIWebsocketTransport` + `PlivoFrameSerializer` with a Rust-backed transport. Same `Pipeline` and `BaseTransport` interface — Pipecat's `FastAPIWebsocketTransport` does audio pacing in Python; `AudioStreamTransport` moves WebSocket handling, codec negotiation, and 20ms audio pacing into Rust for lower jitter at high concurrency.

```python
from agent_transport.audio_stream.pipecat import AudioStreamServer, AudioStreamTransport

server = AudioStreamServer()

@server.handler()
async def run_bot(transport: AudioStreamTransport):
    pipeline = Pipeline([
        transport.input(), stt, user_aggregator, llm, tts,
        transport.output(), assistant_aggregator,
    ])
    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport):
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport):
        await task.cancel()

    await PipelineRunner().run(task)

server.run()
```

Also available for SIP/RTP: `from agent_transport.sip.pipecat import SipTransport`

Full examples: [`audio_stream_agent.py`](examples/pipecat/audio_stream_agent.py) · [`sip_agent.py`](examples/pipecat/sip_agent.py)

## Installation

### Rust Core

No system dependencies — pure Rust.

```bash
cargo build                                     # Core library (SIP transport)
cargo build --features audio-stream             # + Plivo audio streaming
cargo build --features audio-processing         # + jitter buffer, PLC, comfort noise
```

### Python

```bash
# 1. Build the native Rust binding
cd crates/agent-transport-python && pip install -e .

# 2. Install the SIP adapter (LiveKit or Pipecat)
cd python && pip install -e ".[livekit]"        # LiveKit adapter
cd python && pip install -e ".[pipecat]"        # Pipecat adapter
cd python && pip install -e ".[all]"            # Both

# 3. Install LiveKit plugins
pip install livekit-plugins-silero livekit-plugins-deepgram livekit-plugins-openai
pip install livekit-plugins-turn-detector       # Optional: ML-based turn detection
```

### TypeScript / Node.js

```bash
# 1. Build the native Rust binding
cd crates/agent-transport-node && npm run build

# 2. Install the SIP adapter
cd node/agent-transport-sip-livekit && npm install && npm run build

# 3. Install LiveKit plugins
npm install @livekit/agents @livekit/agents-plugin-silero \
  @livekit/agents-plugin-deepgram @livekit/agents-plugin-openai \
  @livekit/agents-plugin-livekit zod
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
