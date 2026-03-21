# LiveKit Audio Streaming Transport Interface

Drop-in replacement for LiveKit's `RoomAudioOutput`/`RoomAudioInput`. Connects a LiveKit `AgentSession` to a Plivo audio stream via WebSocket — no LiveKit server or WebRTC required.

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

## Usage

```python
from agent_transport import AudioStreamEndpoint
from agent_transport_adapters.livekit import AudioStreamInput, AudioStreamOutput
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
