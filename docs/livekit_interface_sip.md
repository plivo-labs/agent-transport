# LiveKit SIP Transport

Drop-in SIP transport for LiveKit Agents. Run the same `AgentSession` pipeline (STT → LLM → TTS) over SIP/RTP instead of WebRTC — no LiveKit server needed.

## Quick Start

```python
from agent_transport.sip.livekit import AgentServer, JobContext, JobProcess
from livekit.agents import Agent, AgentSession, TurnHandlingOptions, metrics
from livekit.agents.voice import MetricsCollectedEvent
from livekit.plugins import deepgram, openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

server = AgentServer(
    sip_username=os.environ["SIP_USERNAME"],
    sip_password=os.environ["SIP_PASSWORD"],
    sip_server=os.environ.get("SIP_DOMAIN", "phone.plivo.com"),
)

def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["turn_detector"] = MultilingualModel()

server.setup_fnc = prewarm

class Assistant(Agent):
    def __init__(self):
        super().__init__(instructions="You are a helpful phone assistant.")

    async def on_enter(self):
        self.session.generate_reply(instructions="Greet the user.")

@server.sip_session()
async def entrypoint(ctx: JobContext):
    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=deepgram.STT(model="nova-3"),
        llm=openai.LLM(model="gpt-4.1-mini"),
        tts=openai.TTS(voice="alloy"),
        turn_handling=TurnHandlingOptions(
            turn_detection=ctx.proc.userdata["turn_detector"],
        ),
        preemptive_generation=True,
        aec_warmup_duration=3.0,
    )
    ctx.session = session

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent):
        metrics.log_metrics(ev.metrics)

    await session.start(agent=Assistant(), room=ctx.room)

if __name__ == "__main__":
    server.run()
```

### Running

```bash
# Production
python agent.py start

# Development (adapter/pipeline debug logs)
python agent.py dev

# Full debug (including Rust SIP/RTP)
python agent.py debug

# Custom port
python agent.py dev --port 9090
```

### Making Outbound Calls

```bash
curl -X POST http://localhost:8080/call \
  -H 'Content-Type: application/json' \
  -d '{"to": "sip:+15551234567@phone.plivo.com"}'
```

---

## AgentServer

Equivalent of LiveKit's `AgentServer` + `cli.run_app()`. Handles SIP registration, call routing, HTTP server, and lifecycle management.

```python
AgentServer(
    sip_server="phone.plivo.com",   # SIP provider domain (or SIP_DOMAIN env)
    sip_port=5060,                  # SIP port (or SIP_PORT env)
    sip_username="user",            # SIP credentials (or SIP_USERNAME env)
    sip_password="pass",            # (or SIP_PASSWORD env)
    host="0.0.0.0",                 # HTTP server bind address
    port=8080,                      # HTTP server port (or PORT env)
    agent_name="sip-agent",         # Agent name for /worker endpoint
    auth=None,                      # Optional auth callable (see Auth section)
    recording=False,                # Enable call recording
    recording_dir="recordings",     # Recording output directory
    recording_stereo=True,          # Stereo (L=user, R=agent) or mono
)
```

### Setup & Decorators

| API | Description |
|-----|-------------|
| `server.setup_fnc = prewarm` | Register a setup function `prewarm(proc: JobProcess)` that runs once at startup. Load models into `proc.userdata`, available as `ctx.proc.userdata` in each call. |
| `@server.sip_session()` | Register the call entrypoint -- equivalent of `@server.rtc_session()` in LiveKit. |

### HTTP Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Health check — returns `200 OK` |
| `GET /worker` | Worker status (agent_name, active_jobs, worker_load, sdk_version) |
| `GET /metrics` | Prometheus metrics (active jobs, CPU load, call count, call duration) |
| `POST /call` | Trigger outbound call: `{"to": "sip:+1555...@provider.com"}` |

### CLI Commands

| Command | Description |
|---------|-------------|
| `start` | Production mode — INFO logging |
| `dev` | Development mode — DEBUG for adapters/pipeline, INFO for Rust |
| `debug` | Full debug — DEBUG everything including Rust SIP/RTP |

### Prometheus Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `lk_agents_active_job_count` | Gauge | Active calls (same as LiveKit) |
| `lk_agents_worker_load` | Gauge | CPU load 0-1 (same as LiveKit) |
| `lk_agents_sip_calls_total` | Counter | Total calls by direction (inbound/outbound) |
| `lk_agents_sip_call_duration_seconds` | Histogram | Call duration distribution |

---

## JobContext

Passed to the `@sip_session()` handler — equivalent of LiveKit's `JobContext`.

| Field | Type | Description |
|-------|------|-------------|
| `call_id` | `str` | SIP call identifier |
| `remote_uri` | `str` | Caller's SIP URI |
| `direction` | `str` | `"inbound"` or `"outbound"` |
| `proc` | `JobProcess` | Process info; `ctx.proc.userdata` has data from `prewarm()` |

### Methods

| Method | Description |
|--------|-------------|
| `ctx.start(session, agent=)` | Wire SIP audio I/O, start the agent, and wait for call end. Equivalent of `session.start(agent=, room=ctx.room)`. |
| `ctx.add_shutdown_callback(cb)` | Register a callback to run when the call ends (cleanup, metrics flush, etc.). |

---

## Call Recording

Stereo OGG/Opus recording at the Rust RTP layer -- efficient compressed output.

```python
server = AgentServer(
    recording=True,                 # Enable recording
    recording_dir="recordings",     # Output directory (default: ./recordings)
    recording_stereo=True,          # True: L=user R=agent, False: mono mix
)
```

Output: `recordings/call_{id}.ogg` -- OGG/Opus, 16kHz.

- User audio captured after G.711 decode + 8->16kHz resample
- Agent audio captured from the AudioBuffer before 16->8kHz downsample
- Opus-encoded in batch, much smaller files than raw PCM

---

## Authentication

Optional callable for protecting `/call`, `/worker`, and `/metrics` endpoints. Health endpoint (`GET /`) is always open.

```python
# Bearer token
server = AgentServer(
    auth=lambda req: req.headers.get("Authorization") == "Bearer my-secret",
)

# Async database lookup
async def check_auth(request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    return await db.validate_token(token)

server = AgentServer(auth=check_auth)
```

---

## Turn Detection

LiveKit's `MultilingualModel` and `EnglishModel` work transparently. The `AgentServer` creates a subprocess-based inference executor (same as LiveKit's `AgentServer`) so the ONNX model runs locally without LiveKit Cloud.

```bash
# Install the turn detector plugin
pip install livekit-plugins-turn-detector

# Download model files
python agent.py download-files
```

```python
def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["turn_detector"] = MultilingualModel()  # or EnglishModel()

server.setup_fnc = prewarm
```

---

## Audio Pipeline Architecture

```
Inbound (user → agent):
  Phone → SIP INVITE → RTP recv → G.711 decode → speexdsp 8→16kHz
    → SipAudioInput → Chan → AgentSession pipeline → VAD + STT

Outbound (agent → user):
  AgentSession → TTS → SipAudioOutput → Chan → SipAudioSource
    → AudioBuffer (Rust, 200ms threshold) → RTP send loop (20ms pacing)
    → speexdsp 16→8kHz → G.711 encode → RTP → Phone
```

### Backpressure

Matches WebRTC C++ `InternalSource` exactly:
- AudioBuffer threshold: 200ms (3200 samples at 16kHz), matching `_ParticipantAudioOutput` production
- Below threshold: completion callback fires immediately
- Above threshold: callback deferred until RTP loop drains below threshold
- Capacity: 400ms (6400 samples) — hard reject above this

### Interruption

1. Pipeline detects user speech → calls `pause()` on audio output
2. `_forward_audio` clears Rust AudioBuffer (first clear, ~15ms)
3. Pipeline commits interrupt → calls `clear_buffer()` → `_interrupted_event` set
4. `_wait_for_playout` detects interrupt → calls `clear_queue()` (second clear)
5. `on_playback_finished(interrupted=True)` emitted

---

## SipAudioInput

Implements `livekit.agents.voice.io.AudioInput`. Async iterator yielding `rtc.AudioFrame` from a SIP call.

- Sample rate: 16000 Hz
- Channels: 1 (mono)
- Frame size: 20ms (320 samples)
- Threading: `recv_audio_bytes_blocking` via `run_in_executor` (GIL released)
- On stream end: pushes 0.5s silence to flush STT (matches LiveKit)

## SipAudioOutput

Implements `livekit.agents.voice.io.AudioOutput`. Line-for-line match of LiveKit's `_ParticipantAudioOutput`:

- `SipAudioSource` replaces `rtc.AudioSource`
- `Chan` replaces `utils.aio.Chan`
- `cancel_and_wait` replaces `utils.aio.cancel_and_wait`
- All methods (capture_frame, flush, clear_buffer, pause, resume, _wait_for_playout, _forward_audio) match WebRTC transport exactly

---

## Examples

| Example | Description |
|---------|-------------|
| [`sip_agent.py`](../examples/livekit/sip_agent.py) | Single agent with tool calling, turn detection, preemptive generation |
| [`sip_agent.ts`](../examples/livekit/sip_agent.ts) | TypeScript SIP agent with tool calling, turn detection, metrics |
| [`sip_multi_agent.py`](../examples/livekit/sip_multi_agent.py) | Multi-agent with greeter -> sales/support handoff and tool calling |
| [`sip_multi_agent.ts`](../examples/livekit/sip_multi_agent.ts) | TypeScript multi-agent with class inheritance and `llm.handoff()` |
| [`audio_stream_agent.py`](../examples/livekit/audio_stream_agent.py) | Agent over Plivo audio streaming (WebSocket) |
| [`audio_stream_agent.ts`](../examples/livekit/audio_stream_agent.ts) | TypeScript audio streaming agent |
| [`audio_stream_multi_agent.py`](../examples/livekit/audio_stream_multi_agent.py) | Audio streaming multi-agent with handoff |
| [`audio_stream_multi_agent.ts`](../examples/livekit/audio_stream_multi_agent.ts) | TypeScript audio streaming multi-agent |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SIP_USERNAME` | — | SIP registration username |
| `SIP_PASSWORD` | — | SIP registration password |
| `SIP_DOMAIN` | `phone.plivo.com` | SIP server domain |
| `SIP_PORT` | `5060` | SIP server port |
| `PORT` | `8080` | HTTP server port |
| `RUST_LOG` | `info` | Rust log level (overrides CLI mode) |
| `DEEPGRAM_API_KEY` | — | Deepgram STT API key |
| `OPENAI_API_KEY` | — | OpenAI LLM/TTS API key |

---

## Import Paths

### Python

```python
from agent_transport.sip.livekit import AgentServer, JobContext, JobProcess, run_app
from agent_transport.sip.livekit import SipAudioInput, SipAudioOutput  # low-level access
```

### TypeScript

```typescript
import { AgentServer, JobContext } from 'agent-transport/livekit';
import { SipAudioInput, SipAudioOutput } from 'agent-transport/livekit'; // low-level
```

---

## TypeScript Quick Start

```typescript
import { AgentServer, type JobContext } from 'agent-transport/livekit';
import { voice, llm, metrics } from '@livekit/agents';
import * as deepgram from '@livekit/agents-plugin-deepgram';
import * as openai from '@livekit/agents-plugin-openai';
import * as silero from '@livekit/agents-plugin-silero';
import * as livekit from '@livekit/agents-plugin-livekit';
import { z } from 'zod';

const server = new AgentServer({
  sipUsername: process.env.SIP_USERNAME!,
  sipPassword: process.env.SIP_PASSWORD!,
});

server.setupFnc = (proc) => {
  proc.userdata.vad = silero.VAD.load();
  proc.userdata.turnDetector = new livekit.turnDetector.MultilingualModel();
};

const agent = new voice.Agent({
  instructions: 'You are a helpful phone assistant.',
  tools: {
    getWeather: llm.tool({
      description: 'Get weather for a location.',
      parameters: z.object({ location: z.string() }),
      execute: async ({ location }) => `Sunny in ${location}, 72°F.`,
    }),
  },
});

server.sipSession(async (ctx: JobContext) => {
  const session = new voice.AgentSession({
    vad: ctx.proc.userdata.vad as silero.VAD,
    stt: new deepgram.STT({ model: 'nova-3' }),
    llm: new openai.LLM({ model: 'gpt-4.1-mini' }),
    tts: new openai.TTS({ voice: 'alloy' }),
    turnHandling: {
      turnDetection: ctx.proc.userdata.turnDetector as livekit.turnDetector.MultilingualModel,
    },
    preemptiveGeneration: true,
    aecWarmupDuration: 3000,
  });

  session.on(voice.AgentSessionEventTypes.MetricsCollected, (ev) => {
    metrics.logMetrics(ev.metrics);
  });

  ctx.session = session;
  await session.start({ agent, room: ctx.room });
  session.say('Hello, how can I help you today?');
});

server.run();
```

### TypeScript Examples

| Example | Description |
|---------|-------------|
| [`sip_agent.ts`](../examples/livekit/sip_agent.ts) | Single agent with tool calling, turn detection, metrics |
| [`sip_multi_agent.ts`](../examples/livekit/sip_multi_agent.ts) | Multi-agent with class inheritance, `llm.handoff()`, per-agent tools |
