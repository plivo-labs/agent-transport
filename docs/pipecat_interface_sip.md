# Pipecat SIP Transport Interface

Drop-in replacement for Pipecat's `WebsocketServerTransport`. Connects a Pipecat pipeline to a SIP call via direct RTP — no WebSocket server or PlivoFrameSerializer needed.

Uses Pipecat's `BaseInputTransport` / `BaseOutputTransport` / `BaseTransport` base classes with full MediaSender infrastructure for audio chunking, bot speaking state, and interruption handling.

## Classes

### SipTransport

Implements `pipecat.transports.base_transport.BaseTransport`. Top-level transport that creates input and output processors for a pipeline.

```
SipTransport(endpoint, call_id, *, name="SipTransport", params=None)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `endpoint` | `SipEndpoint` | The Rust SIP endpoint instance |
| `call_id` | `int` | Call ID |
| `name` | `str` | Transport name for debugging |
| `params` | `TransportParams \| None` | Optional override for default transport parameters |

#### Methods

| Method | Signature | Description |
|--------|-----------|-------------|
| `input` | `() → SipInputTransport` | Returns the input processor (creates lazily) |
| `output` | `() → SipOutputTransport` | Returns the output processor (creates lazily) |

#### Pipeline Integration

```python
transport = SipTransport(ep, call_id)
pipeline = Pipeline([
    transport.input(),    # ← receives audio from SIP
    stt_service,
    llm_service,
    tts_service,
    transport.output(),   # ← sends audio to SIP
])
```

---

### SipInputTransport

Extends `pipecat.transports.base_input.BaseInputTransport`. Receives audio and events from a SIP call.

#### Constructor Parameters (via SipTransport or direct)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `endpoint` | `SipEndpoint` | required | Rust SIP endpoint |
| `call_id` | `int` | required | Call ID |
| `params` | `TransportParams` | `audio_in_enabled=True, audio_in_passthrough=True` | Transport configuration |

#### Default TransportParams

```python
TransportParams(
    audio_in_enabled=True,       # Enable audio input processing
    audio_in_passthrough=True,   # Pass audio frames downstream through pipeline
)
```

#### Lifecycle

| Method | Description |
|--------|-------------|
| `start(StartFrame)` | Calls `super().start()`, `set_transport_ready()` (creates internal audio queue and task), then starts audio recv loop and event polling loop |
| `stop(EndFrame)` | Cancels recv/event tasks, calls `super().stop()` |
| `cancel(CancelFrame)` | Same as stop but via cancel path |

#### Input Frames Produced

| Frame | Source | Description |
|-------|--------|-------------|
| `InputAudioRawFrame` | Audio recv loop | 16kHz mono int16 PCM, 20ms frames. Rust decodes G.711 and upsamples. Pushed via `push_audio_frame()` through the base class audio queue. |
| `InputDTMFFrame` | Event polling loop | DTMF digits received via RTP (RFC 2833). Uses `KeypadEntry` enum. |
| `EndFrame` | Event polling loop | When SIP BYE is received (call terminated by remote). |

#### Threading Model

- Audio recv: `run_in_executor(recv_audio_bytes_blocking, 20ms)` — GIL released
- Event poll: `run_in_executor(wait_for_event, 100ms)` — GIL released
- Both loops exit cleanly on session removal (catch exception, break)

---

### SipOutputTransport

Extends `pipecat.transports.base_output.BaseOutputTransport`. Sends audio and handles output frames for a SIP call.

#### Constructor Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `endpoint` | `SipEndpoint` | required | Rust SIP endpoint |
| `call_id` | `int` | required | Call ID |
| `params` | `TransportParams` | `audio_out_enabled=True` | Transport configuration |

#### Lifecycle

| Method | Description |
|--------|-------------|
| `start(StartFrame)` | Calls `super().start()`, `set_transport_ready()`. Creates MediaSender with audio task, clock task. Pushes `OutputTransportReadyFrame` upstream. |
| `stop(EndFrame)` | Hangs up the SIP call (sends BYE via `run_in_executor`), then `super().stop()` drains MediaSender |
| `cancel(CancelFrame)` | Same hangup + cancel path |

#### Output Frame Handling

| Frame | Handler | Description |
|-------|---------|-------------|
| `OutputAudioRawFrame` | `write_audio_frame` | Sends PCM bytes to Rust via `send_audio_bytes`. Rust encodes to G.711, paces at 20ms via `tokio::time::interval`, sends as RTP. MediaSender handles chunking and resampling before this is called. |
| `InterruptionFrame` | `process_frame` | Calls `clear_buffer` on Rust endpoint (clears outgoing RTP queue). Then base class cancels/restarts MediaSender tasks and emits `BotStoppedSpeakingFrame`. |
| `OutputDTMFFrame` / `OutputDTMFUrgentFrame` | `_write_dtmf_native` | Sends RFC 2833 DTMF events via `run_in_executor` (GIL released). |
| `OutputTransportMessageFrame` / Urgent | `send_message` | Sends JSON as SIP INFO message body (`application/json` content type) via `run_in_executor`. Filters RTVI messages (`label == "rtvi-ai"`). |
| `EndFrame` / `CancelFrame` | `stop` / `cancel` | Hangs up the SIP call. |

#### MediaSender Infrastructure (from BaseOutputTransport)

By calling `set_transport_ready()`, the base class creates a MediaSender that provides:

- **Audio chunking**: Large TTS frames are split into 10ms × N chunks before `write_audio_frame` is called
- **Audio resampling**: If TTS produces audio at a different rate than 16kHz, MediaSender resamples
- **Bot speaking state**: `BotStartedSpeakingFrame` / `BotStoppedSpeakingFrame` emitted automatically based on audio activity
- **Interruption handling**: On `InterruptionFrame`, MediaSender cancels and restarts its internal audio task

#### DTMF Support

Native DTMF via RFC 2833 RTP telephone-event. Pipecat's base class routes:
- `OutputDTMFUrgentFrame` → immediate send via `_write_dtmf_native`
- `OutputDTMFFrame` → queued through MediaSender audio task, then `_write_dtmf_native`

---

## TransportParams Reference

| Parameter | Default | Description |
|-----------|---------|-------------|
| `audio_in_enabled` | `True` | Enable audio input (must be True) |
| `audio_in_passthrough` | `True` | Pass audio downstream (must be True for STT to receive audio) |
| `audio_in_sample_rate` | `None` | Override input sample rate (None = use StartFrame value) |
| `audio_out_enabled` | `True` | Enable audio output (must be True) |
| `audio_out_sample_rate` | `None` | Override output sample rate (None = use StartFrame value) |
| `audio_out_channels` | `1` | Output audio channels |
| `audio_out_10ms_chunks` | `4` | Number of 10ms chunks per write (affects chunking granularity) |

## Usage

```python
from agent_transport import SipEndpoint
from agent_transport_adapters.pipecat import SipTransport
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask

ep = SipEndpoint(sip_server="phone.plivo.com")
ep.register(username, password)
# ... wait for incoming call, answer, wait for media active ...

transport = SipTransport(ep, call_id)
pipeline = Pipeline([transport.input(), stt, llm, tts, transport.output()])
task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))
runner = PipelineRunner()
await runner.run(task)
```
