# Pipecat Audio Streaming Transport Interface

Drop-in replacement for Pipecat's `WebsocketServerTransport` + `PlivoFrameSerializer`. Connects a Pipecat pipeline to a Plivo audio stream — all codec/resampling/pacing handled in Rust.

Uses Pipecat's `BaseInputTransport` / `BaseOutputTransport` / `BaseTransport` base classes with full MediaSender infrastructure.

## Classes

### AudioStreamTransport

Implements `pipecat.transports.base_transport.BaseTransport`. Top-level transport for Plivo audio streaming.

```
AudioStreamTransport(endpoint, session_id, *, name="AudioStreamTransport", params=None)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `endpoint` | `AudioStreamEndpoint` | The Rust audio streaming endpoint instance |
| `session_id` | `int` | Session ID from the `incoming_call` event |
| `name` | `str` | Transport name for debugging |
| `params` | `TransportParams \| None` | Optional override for default transport parameters |

#### Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `input` | `() → AudioStreamInputTransport` | Returns the input processor |
| `output` | `() → AudioStreamOutputTransport` | Returns the output processor |

#### Pipeline Integration

```python
transport = AudioStreamTransport(ep, session_id)
pipeline = Pipeline([
    transport.input(),    # ← receives audio from Plivo WebSocket
    stt_service,
    llm_service,
    tts_service,
    transport.output(),   # ← sends audio to Plivo WebSocket
])
```

---

### AudioStreamInputTransport

Extends `pipecat.transports.base_input.BaseInputTransport`. Receives audio and DTMF from a Plivo audio stream.

#### Default TransportParams

```python
TransportParams(
    audio_in_enabled=True,
    audio_in_passthrough=True,
)
```

#### Lifecycle

| Method | Description |
|--------|-------------|
| `start(StartFrame)` | Creates internal audio queue (via `set_transport_ready`), starts audio recv loop and DTMF/event polling loop |
| `stop(EndFrame)` | Cancels both loops, calls base class stop |
| `cancel(CancelFrame)` | Same via cancel path |

#### Input Frames Produced

| Frame | Source | Description |
|-------|--------|-------------|
| `InputAudioRawFrame` | Audio recv loop | 16kHz mono int16 PCM. Rust handles all decoding: mu-law → PCM or L16 byte-swap, plus 8kHz→16kHz upsampling. |
| `InputDTMFFrame` | Event poll loop | DTMF digits from Plivo `dtmf` JSON events. Constructed as `InputDTMFFrame(button=KeypadEntry(digit))`. |
| `EndFrame` | Event poll loop | When Plivo sends `stop` event (stream ended) or call is terminated. |

#### Plivo Audio Formats Handled (by Rust)

| Plivo `mediaFormat` | Rust Decoding |
|--------------------|---------------|
| `audio/x-mulaw;rate=8000` (default) | mu-law decode → upsample 8k→16k |
| `audio/x-l16;rate=8000` | LE int16 decode → upsample 8k→16k |
| `audio/x-l16;rate=16000` | LE int16 decode (native) |

Format is auto-detected from Plivo's `start` event `mediaFormat` field.

---

### AudioStreamOutputTransport

Extends `pipecat.transports.base_output.BaseOutputTransport`. Sends audio and handles output frames for a Plivo audio stream.

#### Default TransportParams

```python
TransportParams(
    audio_out_enabled=True,
)
```

#### Lifecycle

| Method | Description |
|--------|-------------|
| `start(StartFrame)` | Creates MediaSender (via `set_transport_ready`). MediaSender manages audio chunking, bot speaking state, and internal task lifecycle. |
| `stop(EndFrame)` | Hangs up via Plivo REST API DELETE (`run_in_executor`, GIL released), then base class drains MediaSender |
| `cancel(CancelFrame)` | Same hangup + cancel path |

#### Output Frame Handling

| Frame | Handler | Description |
|-------|---------|-------------|
| `OutputAudioRawFrame` | `write_audio_frame` | Sends PCM to Rust via `send_audio_bytes`. Rust encodes to Plivo's negotiated format, base64-encodes, wraps in `playAudio` JSON, sends over WebSocket with 20ms `tokio::time::interval` pacing. |
| `InterruptionFrame` | `process_frame` | Calls `clear_buffer` (Rust drains local queue + sends `clearAudio` JSON to Plivo). Then base class restarts MediaSender and emits `BotStoppedSpeakingFrame`. |
| `OutputDTMFFrame` / Urgent | `_write_dtmf_native` | Sends `sendDTMF` JSON command to Plivo via Rust `send_dtmf`. Non-blocking (channel send). |
| `OutputTransportMessageFrame` / Urgent | `send_message` | Sends JSON over the Plivo WebSocket via `send_raw_message`. Filters RTVI internal messages (`message.label == "rtvi-ai"`). |
| `EndFrame` / `CancelFrame` | `stop` / `cancel` | Hangs up via Plivo REST API. |

#### MediaSender Infrastructure

Provided automatically by `set_transport_ready()`:

| Feature | Description |
|---------|-------------|
| Audio chunking | TTS output split into 10ms × N chunks before `write_audio_frame` |
| Audio resampling | Incoming audio resampled to transport's 16kHz if needed |
| Bot speaking state | `BotStartedSpeakingFrame` / `BotStoppedSpeakingFrame` emitted based on audio activity with 350ms silence threshold |
| `BotSpeakingFrame` | Periodic frame emitted every 200ms while bot is speaking |
| Interruption handling | On `InterruptionFrame`, cancels and restarts audio/clock/video tasks |
| End-of-stream silence | Configurable silence padding on EndFrame (`audio_out_end_silence_secs`) |

#### DTMF Support

Native DTMF via Plivo `sendDTMF` command. The Rust endpoint sends `{"event": "sendDTMF", "dtmf": "5"}` over the WebSocket. No audio tone generation needed.

#### RTVI Message Filtering

`send_message` filters RTVI internal protocol messages before sending to Plivo:

```python
if message.get("label") == "rtvi-ai":
    return  # Don't send RTVI messages over the Plivo WebSocket
```

This matches Pipecat's `FrameSerializer.should_ignore_frame` behavior. Non-RTVI messages are forwarded as raw JSON over the WebSocket via `send_raw_message`.

#### Extra Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `queued_frames` | `() → int` | Number of audio frames in the Rust outgoing queue. Multiply by 0.02 for seconds. |

---

## Plivo WebSocket Protocol (handled by Rust)

### Inbound Events (Plivo → Rust)

| Event | Description |
|-------|-------------|
| `start` | Connection established. Contains `callId`, `streamId`, `mediaFormat`, `extra_headers`. |
| `media` | Audio data. Base64-encoded payload in negotiated format. |
| `dtmf` | DTMF digit received. |
| `playedStream` | Checkpoint confirmation — all audio up to named checkpoint has played. |
| `clearedAudio` | Confirmation that server-side audio buffer was cleared. |
| `stop` | Stream ended (call hung up or stream closed). |

### Outbound Commands (Rust → Plivo)

| Command | Triggered by | JSON |
|---------|-------------|------|
| `playAudio` | `write_audio_frame` | `{"event": "playAudio", "media": {"contentType": "audio/x-mulaw", "sampleRate": 8000, "payload": "..."}}` |
| `clearAudio` | `clear_buffer` | `{"event": "clearAudio", "streamId": "..."}` |
| `checkpoint` | `flush` | `{"event": "checkpoint", "streamId": "...", "name": "cp-0"}` |
| `sendDTMF` | `_write_dtmf_native` | `{"event": "sendDTMF", "dtmf": "5"}` |

### Hangup

Plivo REST API: `DELETE https://api.plivo.com/v1/Account/{auth_id}/Call/{call_id}/`

Called on `stop()` / `cancel()` / `shutdown()`. Idempotent — returns 204 (success) or 404 (already ended).

---

## Usage

```python
from agent_transport import AudioStreamEndpoint
from agent_transport_adapters.pipecat import AudioStreamTransport
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask

ep = AudioStreamEndpoint(
    listen_addr="0.0.0.0:8080",
    plivo_auth_id="...",
    plivo_auth_token="...",
)
# ... wait for incoming_call event, get session_id ...

transport = AudioStreamTransport(ep, session_id)
pipeline = Pipeline([transport.input(), stt, llm, tts, transport.output()])
task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))
runner = PipelineRunner()
await runner.run(task)
```
