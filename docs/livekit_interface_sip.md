# LiveKit SIP Transport Interface

Drop-in replacement for LiveKit's `RoomAudioOutput`/`RoomAudioInput`. Connects a LiveKit `AgentSession` to a SIP call via direct RTP — no LiveKit server or WebRTC required.

## Classes

### SipAudioInput

Implements `livekit.agents.voice.io.AudioInput`. Async iterator that yields `rtc.AudioFrame` from a SIP call.

```
SipAudioInput(endpoint, call_id, *, label="sip-audio-input", source=None)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `endpoint` | `SipEndpoint` | The Rust SIP endpoint instance |
| `call_id` | `int` | Call ID returned by `endpoint.call()` or from `incoming_call` event |
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
| `__aiter__` | `() → AsyncIterator[AudioFrame]` | Returns self (async iterator protocol) |
| `__anext__` | `() → AudioFrame` | Blocks in a thread pool (20ms timeout) until an audio frame arrives from the SIP call. Returns 16kHz mono int16 PCM wrapped in `rtc.AudioFrame`. Raises `StopAsyncIteration` when detached or session removed. |
| `on_attached` | `() → None` | Called by LiveKit when this input is connected to a session. Propagates to `source` if set. |
| `on_detached` | `() → None` | Called by LiveKit when disconnected. Stops the audio iterator. Propagates to `source` if set. |

#### Audio Format

- Sample rate: 16000 Hz (Rust upsamples from 8kHz G.711)
- Channels: 1 (mono)
- Encoding: int16 PCM (little-endian)
- Frame size: 20ms (320 samples)

#### Threading Model

`__anext__` uses `asyncio.run_in_executor` to call `recv_audio_bytes_blocking(call_id, 20)` in a thread pool. The Rust binding releases the Python GIL during the blocking wait. The asyncio event loop is never blocked.

---

### SipAudioOutput

Implements `livekit.agents.voice.io.AudioOutput`. Sends audio frames to a SIP call with segment tracking, playout detection, interruption handling, and pause/resume.

```
SipAudioOutput(endpoint, call_id, *, label="sip-audio-output",
               capabilities=None, sample_rate=None, next_in_chain=None)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `endpoint` | `SipEndpoint` | The Rust SIP endpoint instance |
| `call_id` | `int` | Call ID |
| `label` | `str` | Human-readable label |
| `capabilities` | `AudioOutputCapabilities \| None` | Defaults to `AudioOutputCapabilities(pause=True)` |
| `sample_rate` | `int \| None` | Ignored — always returns the endpoint's native rate (16kHz) |
| `next_in_chain` | `AudioOutput \| None` | Optional downstream output for chaining |

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `label` | `str` | Inherited from base class |
| `sample_rate` | `int` | Always 16000 (from Rust endpoint) |
| `can_pause` | `bool` | True if `capabilities.pause` and all chain members support pause |
| `next_in_chain` | `AudioOutput \| None` | Inherited from base class |

#### Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `capture_frame` | `async (frame: AudioFrame) → None` | Sends one audio frame to the SIP call via `send_audio_bytes`. Increments segment count on first call per segment (via base class). Emits `playback_started` event after the first frame of each segment. |
| `flush` | `() → None` | Marks the current segment as complete. Sends a flush signal to Rust. Starts an async background task that races playout completion vs interruption, then emits `playback_finished`. |
| `clear_buffer` | `() → None` | Immediately clears all buffered audio in the Rust outgoing queue. If a flush is in progress, signals interruption (the flush task computes partial playback position and emits `playback_finished(interrupted=True)`). If no flush is in progress, emits `playback_finished` directly. |
| `pause` | `() → None` | Pauses audio playback. Frames accumulate in the Rust buffer (up to 360 seconds). Propagates to `next_in_chain`. |
| `resume` | `() → None` | Resumes audio playback. Buffered frames start sending. Propagates to `next_in_chain`. |
| `wait_for_playout` | `async () → PlaybackFinishedEvent` | Inherited from base class. Awaits until all captured segments have finished playing (or been interrupted). |
| `on_attached` | `() → None` | Propagates to `next_in_chain`. |
| `on_detached` | `() → None` | Cancels any in-progress flush task. Propagates to `next_in_chain`. |

#### Events

| Event | Payload | When |
|-------|---------|------|
| `playback_started` | `PlaybackStartedEvent(created_at: float)` | After the first frame of a segment is sent to Rust |
| `playback_finished` | `PlaybackFinishedEvent(playback_position: float, interrupted: bool)` | After playout completes or `clear_buffer` interrupts |

#### Segment Lifecycle

```
capture_frame(f1)  → segment starts, playback_started emitted
capture_frame(f2)
capture_frame(f3)
flush()            → segment ends, async playout wait starts
                   → ...Rust plays audio over RTP...
                   → playback_finished(playback_position=0.06, interrupted=False)
```

Interruption:
```
capture_frame(f1)
capture_frame(f2)
flush()            → async playout wait starts
clear_buffer()     → Rust clears queue, playback_finished(interrupted=True)
```

#### Threading Model

- `capture_frame`: `send_audio_bytes` is a non-blocking channel push (instant). Called from asyncio coroutine.
- `flush`: Starts `_async_wait_for_playout` task. The task uses `run_in_executor` + `py.allow_threads` for the blocking `wait_for_playout` call.
- `clear_buffer`, `pause`, `resume`: Instant (atomic flag operations in Rust).

---

## Usage

```python
from agent_transport import SipEndpoint
from agent_transport_adapters.livekit import SipAudioInput, SipAudioOutput
from livekit.agents.voice import AgentSession, Agent

ep = SipEndpoint(sip_server="phone.plivo.com")
ep.register(username, password)
# ... wait for incoming call, get call_id ...

session = AgentSession(stt=..., llm=..., tts=...)
session.input.audio = SipAudioInput(ep, call_id)
session.output.audio = SipAudioOutput(ep, call_id)
await session.start(agent=MyAgent())
```

Do not pass `room=` to `session.start()` — this tells LiveKit to skip RoomIO and use the custom I/O you set.
