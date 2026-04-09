# LiveKit Audio Streaming Transport

Drop-in audio streaming transport for LiveKit Agents. Run the same `AgentSession` pipeline (STT → LLM → TTS) over Plivo WebSocket audio streaming — no LiveKit server or WebRTC required.

## Quick Start

```python
from agent_transport.audio_stream.livekit import AudioStreamServer, JobContext, JobProcess
from livekit.agents import Agent, AgentSession, TurnHandlingOptions, metrics
from livekit.agents.voice import MetricsCollectedEvent
from livekit.plugins import deepgram, openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

server = AudioStreamServer(
    listen_addr=os.environ.get("AUDIO_STREAM_ADDR", "0.0.0.0:8765"),
    plivo_auth_id=os.environ.get("PLIVO_AUTH_ID", ""),
    plivo_auth_token=os.environ.get("PLIVO_AUTH_TOKEN", ""),
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

@server.audio_stream_session()
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

### Setup (Plivo XML)

Configure your Plivo application's answer URL to return:

```xml
<Response>
    <Stream bidirectional="true" keepCallAlive="true"
        contentType="audio/x-mulaw;rate=8000">
        wss://your-server:8765
    </Stream>
</Response>
```

### Running

```bash
python agent.py start    # Production (INFO logging)
python agent.py dev      # Development (DEBUG for adapters)
python agent.py debug    # Full debug
```

---

## AudioStreamServer

Equivalent of LiveKit's `AgentServer`. Handles WebSocket connections from Plivo, call lifecycle, HTTP server, and Prometheus metrics.

```python
AudioStreamServer(
    listen_addr="0.0.0.0:8765",     # WebSocket server address
    plivo_auth_id="",               # Plivo auth (or PLIVO_AUTH_ID env)
    plivo_auth_token="",            # Plivo token (or PLIVO_AUTH_TOKEN env)
    host="0.0.0.0",                 # HTTP server bind address
    port=8080,                      # HTTP server port (or PORT env)
    agent_name="audio-stream-agent", # Agent name for /worker endpoint
    recording=False,                # Enable OGG/Opus recording
    recording_dir="recordings",     # Recording output directory
)
```

### Setup & Decorators

| API | Description |
|-----|-------------|
| `server.setup_fnc = prewarm` | Register a setup function `prewarm(proc: JobProcess)`. Load models into `proc.userdata`. |
| `@server.audio_stream_session()` | Register the session entrypoint — equivalent of `@server.rtc_session()`. |

### HTTP Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Health check — returns `200 OK` |
| `GET /worker` | Worker status (agent_name, active_jobs, worker_load) |
| `GET /metrics` | Prometheus metrics (active sessions, CPU load, session count) |

---

## JobContext

Passed to the `@audio_stream_session()` handler — equivalent of LiveKit's `JobContext`.

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | `str` | Plivo stream session ID |
| `call_id` | `str` | Plivo Call UUID |
| `direction` | `str` | Always `"inbound"` |
| `proc` | `JobProcess` | Process info; `ctx.proc.userdata` has data from `prewarm()` |
| `room` | `TransportRoom` | Room facade — `session.start(room=ctx.room)` |

### Methods

| Method | Description |
|--------|-------------|
| `ctx.session = session` | Wire audio stream I/O and register close handler. |
| `ctx.add_shutdown_callback(cb)` | Register a callback to run when the session ends. |

---

## Examples

| Example | Description |
|---------|-------------|
| [`audio_stream_agent.py`](../examples/livekit/audio_stream_agent.py) | Single agent with tool calling, DTMF, metrics |
| [`audio_stream_agent.ts`](../examples/livekit/audio_stream_agent.ts) | TypeScript audio streaming agent |
| [`audio_stream_multi_agent.py`](../examples/livekit/audio_stream_multi_agent.py) | Multi-agent with handoff and tool calling |
| [`audio_stream_multi_agent.ts`](../examples/livekit/audio_stream_multi_agent.ts) | TypeScript audio streaming multi-agent |

---

## Import Paths

### Python

```python
from agent_transport.audio_stream.livekit import AudioStreamServer, JobContext, JobProcess
```

### TypeScript

```typescript
import { AudioStreamServer, JobProcess, type AudioStreamJobContext } from 'agent-transport/livekit';
```

---

## Low-Level I/O Classes

For advanced use cases, the low-level `AudioStreamInput` and `AudioStreamOutput` classes are available. The `AudioStreamServer` uses these internally — most users don't need them directly.

## Classes

### AudioStreamInput

Implements `livekit.agents.voice.io.AudioInput`. Async iterator that yields `rtc.AudioFrame` from a Plivo audio stream.

```
AudioStreamInput(endpoint, session_id, *, label="audio-stream-input", source=None)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `endpoint` | `AudioStreamEndpoint` | The Rust audio streaming endpoint instance |
| `session_id` | `int` | Session ID from the `incoming_call` event |
| `label` | `str` | Human-readable label for pipeline debugging |
| `source` | `AudioInput \| None` | Optional upstream source to delegate to |

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `label` | `str` | The label passed at construction |
| `source` | `AudioInput \| None` | The upstream source, if any |

#### Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `__aiter__` | `() → AsyncIterator[AudioFrame]` | Returns self |
| `__anext__` | `() → AudioFrame` | Blocks in a thread pool (20ms timeout) until an audio frame arrives. Rust decodes mu-law/L16 from Plivo and delivers 16kHz mono int16 PCM. Raises `StopAsyncIteration` when detached or session removed. |
| `on_attached` | `() → None` | Propagates to `source` if set. |
| `on_detached` | `() → None` | Stops the audio iterator. Propagates to `source`. |

#### Audio Format

- Sample rate: 16000 Hz (Rust decodes and upsamples from Plivo's 8kHz mu-law, 8kHz L16, or native 16kHz L16)
- Channels: 1 (mono)
- Encoding: int16 PCM (little-endian)

#### Plivo Audio Format Support

| Plivo Format | Rust Handling |
|-------------|---------------|
| `audio/x-mulaw;rate=8000` | mu-law decode → upsample 8k→16k |
| `audio/x-l16;rate=8000` | LE int16 decode → upsample 8k→16k |
| `audio/x-l16;rate=16000` | LE int16 decode (native, no resampling) |

---

### AudioStreamOutput

Implements `livekit.agents.voice.io.AudioOutput`. Sends audio frames to a Plivo audio stream with segment tracking, checkpoint-based playout detection, interruption handling, and pause/resume.

```
AudioStreamOutput(endpoint, session_id, *, label="audio-stream-output",
                  capabilities=None, sample_rate=None, next_in_chain=None)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `endpoint` | `AudioStreamEndpoint` | The Rust audio streaming endpoint instance |
| `session_id` | `int` | Session ID |
| `label` | `str` | Human-readable label |
| `capabilities` | `AudioOutputCapabilities \| None` | Defaults to `AudioOutputCapabilities(pause=True)` |
| `sample_rate` | `int \| None` | Ignored — returns the endpoint's configured rate (default 16kHz) |
| `next_in_chain` | `AudioOutput \| None` | Optional downstream output for chaining |

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `label` | `str` | Inherited from base class |
| `sample_rate` | `int` | From Rust endpoint config (default 16000) |
| `can_pause` | `bool` | True if capabilities and chain support pause |
| `next_in_chain` | `AudioOutput \| None` | Inherited from base class |

#### Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `capture_frame` | `async (frame: AudioFrame) → None` | Sends one audio frame via `send_audio_bytes`. Rust encodes to the Plivo-negotiated format (mu-law/L16), base64-encodes, wraps in `playAudio` JSON, and sends over WebSocket with 20ms pacing. |
| `flush` | `() → None` | Marks segment complete. Sends a `checkpoint` command to Plivo. Starts async task that waits for Plivo's `playedStream` response (confirming all audio played), then emits `playback_finished`. |
| `clear_buffer` | `() → None` | Drains the Rust outgoing queue AND sends `clearAudio` JSON to Plivo (clears server-side buffer). Emits `playback_finished(interrupted=True)`. |
| `pause` | `() → None` | Pauses — Rust send loop outputs nothing, frames accumulate (360s buffer). |
| `resume` | `() → None` | Resumes — buffered frames start sending. |
| `wait_for_playout` | `async () → PlaybackFinishedEvent` | Inherited. Awaits all segments. |
| `send_raw_message` | `(message: str) → None` | Sends an arbitrary text message over the Plivo WebSocket. |
| `on_attached` | `() → None` | Propagates to `next_in_chain`. |
| `on_detached` | `() → None` | Cancels in-progress flush task. Propagates to `next_in_chain`. |

#### Events

| Event | Payload | When |
|-------|---------|------|
| `playback_started` | `PlaybackStartedEvent(created_at)` | After first frame of segment sent to Rust |
| `playback_finished` | `PlaybackFinishedEvent(playback_position, interrupted)` | After Plivo confirms playout (`playedStream`) or `clear_buffer` interrupts |

#### Playout Detection

Unlike SIP (which uses RTP queue depth), audio streaming uses Plivo's checkpoint protocol:

1. `flush()` sends `{"event": "checkpoint", "streamId": "...", "name": "cp-0"}` to Plivo
2. Plivo responds with `{"event": "playedStream", "name": "cp-0"}` when all audio up to that checkpoint has played
3. The adapter detects this response and emits `playback_finished`

This gives accurate, server-confirmed playout timing.

#### Plivo WebSocket Commands (sent by Rust)

| Command | When | JSON |
|---------|------|------|
| `playAudio` | Each 20ms audio frame | `{"event": "playAudio", "media": {"contentType": "...", "sampleRate": N, "payload": "base64..."}}` |
| `clearAudio` | `clear_buffer()` called | `{"event": "clearAudio", "streamId": "..."}` |
| `checkpoint` | `flush()` called | `{"event": "checkpoint", "streamId": "...", "name": "cp-N"}` |
| `sendDTMF` | DTMF sent | `{"event": "sendDTMF", "dtmf": "5"}` |

---

## Low-Level Usage

For direct control without `AudioStreamServer`:

```python
from agent_transport import AudioStreamEndpoint
from agent_transport.audio_stream.livekit import AudioStreamInput, AudioStreamOutput
from livekit.agents.voice import AgentSession, Agent

ep = AudioStreamEndpoint(
    listen_addr="0.0.0.0:8080",
    plivo_auth_id="...",
    plivo_auth_token="...",
)
# ... wait for incoming_call event, get session_id ...

session = AgentSession(stt=..., llm=..., tts=...)
session.input.audio = AudioStreamInput(ep, session_id)
session.output.audio = AudioStreamOutput(ep, session_id)
await session.start(agent=MyAgent())
```
