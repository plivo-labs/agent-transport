"""Pipecat BaseTransport adapter for Plivo audio streaming.

Replaces Pipecat's WebsocketServerTransport + PlivoFrameSerializer entirely.
All audio codec/resampling/pacing is handled in Rust. Python only bridges frames.

100% compatible with Pipecat's transport interface and Plivo AudioStream protocol:
- Event handlers: on_client_connected, on_client_disconnected
- Session metadata: call_id, stream_id, extra_headers
- All Plivo features: mute, recording, checkpoint, background audio, etc.

Frame handling:
- OutputAudioRawFrame → send_audio_bytes (Rust encodes + paces at 20ms)
- InterruptionFrame → clear_buffer (sends clearAudio to Plivo)
- OutputDTMFFrame → send_dtmf (sends sendDTMF to Plivo)
- OutputTransportMessageFrame → send_raw_message (JSON pass-through over WS)
- EndFrame/CancelFrame → hangup (REST API DELETE)
- InputAudioRawFrame ← recv_audio_bytes_blocking (Rust decodes + resamples)
- InputDTMFFrame ← event polling (from Plivo dtmf events)

Bot speaking state (BotStartedSpeaking/BotStoppedSpeaking) is handled by
Pipecat's BaseOutputTransport MediaSender infrastructure — we call
set_transport_ready() which creates the MediaSender with audio task,
chunking, and bot speaking detection.
"""

import asyncio
import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

try:
    from pipecat.audio.dtmf.types import KeypadEntry
    from pipecat.frames.frames import (
        CancelFrame, EndFrame, Frame, InputAudioRawFrame,
        InputDTMFFrame, InterruptionFrame, OutputAudioRawFrame,
        OutputTransportMessageFrame, OutputTransportMessageUrgentFrame,
        StartFrame, StopFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection
    from pipecat.transports.base_input import BaseInputTransport
    from pipecat.transports.base_output import BaseOutputTransport
    from pipecat.transports.base_transport import BaseTransport, TransportParams
except ImportError:
    raise ImportError("pipecat-ai is required: pip install pipecat-ai")


# ─── Input Transport ────────────────────────────────────────────────────────


class AudioStreamInputTransport(BaseInputTransport):
    """Receives audio + DTMF from Plivo audio stream.

    Audio is received from the Rust endpoint via blocking recv (GIL released),
    decoded and resampled in Rust, and pushed as InputAudioRawFrame into the
    Pipecat pipeline. DTMF and call lifecycle events are polled separately.
    """

    def __init__(self, endpoint, session_id: str, transport: "AudioStreamTransport",
                 params: Optional[TransportParams] = None, **kwargs):
        if params is None:
            params = TransportParams(
                audio_in_enabled=True,
                audio_in_passthrough=True,
            )
        super().__init__(params, **kwargs)
        self._ep = endpoint
        self._sid = session_id
        self._transport = transport
        self._running = False
        self._recv_task: Optional[asyncio.Task] = None
        self._event_task: Optional[asyncio.Task] = None

    async def start(self, frame: StartFrame):
        if self._running:
            return
        await super().start(frame)
        self._running = True
        await self.set_transport_ready(frame)
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._event_task = asyncio.create_task(self._event_loop())
        await self._transport._call_event_handler("on_client_connected", self._transport)

    async def stop(self, frame: EndFrame):
        if not self._running:
            await super().stop(frame)
            return
        self._running = False
        await self._cancel_tasks()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        self._running = False
        await self._cancel_tasks()
        await super().cancel(frame)

    async def pause(self, frame: StopFrame):
        self._running = False
        await self._cancel_tasks()
        await super().pause(frame)

    async def _cancel_tasks(self):
        """Cancel and await background tasks."""
        tasks = [t for t in [self._recv_task, self._event_task] if t and not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._recv_task = None
        self._event_task = None

    async def _recv_loop(self):
        """Receive audio from Rust endpoint via blocking call (GIL released)."""
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                result = await loop.run_in_executor(
                    None, lambda: self._ep.recv_audio_bytes_blocking(self._sid, 20)
                )
            except Exception:
                break  # Session removed — exit cleanly
            if result is not None:
                audio_bytes, sample_rate, num_channels = result
                await self.push_audio_frame(InputAudioRawFrame(
                    audio=bytes(audio_bytes), sample_rate=sample_rate, num_channels=num_channels,
                ))

    async def _event_loop(self):
        """Poll for DTMF, call state, and lifecycle events from Plivo."""
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                event = await loop.run_in_executor(
                    None, lambda: self._ep.wait_for_event(timeout_ms=100)
                )
            except Exception:
                break  # Endpoint shut down — exit cleanly
            if event is None:
                continue
            event_type = event.get("type", "")
            if event_type == "dtmf_received":
                await self.push_frame(InputDTMFFrame(button=KeypadEntry(event["digit"])))
            elif event_type == "call_terminated":
                self._running = False
                await self._transport._call_event_handler("on_client_disconnected", self._transport)
                await self.push_frame(EndFrame())
            elif event_type == "beep_detected":
                await self._transport._call_event_handler(
                    "on_beep_detected", self._transport,
                    frequency_hz=event.get("frequency_hz"),
                    duration_ms=event.get("duration_ms"),
                )
            elif event_type == "beep_timeout":
                await self._transport._call_event_handler("on_beep_timeout", self._transport)


# ─── Output Transport ───────────────────────────────────────────────────────


class AudioStreamOutputTransport(BaseOutputTransport):
    """Sends audio to Plivo audio stream.

    Uses Pipecat's MediaSender infrastructure (via set_transport_ready) for:
    - Audio chunking to transport chunk size
    - BotStartedSpeaking/BotStoppedSpeaking state management
    - Proper interruption handling (task cancellation + restart)

    Audio is sent to the Rust endpoint which handles:
    - Backpressure-aware buffering (200ms threshold)
    - 20ms paced send loop (tokio::time::interval)
    - Encoding negotiation (mulaw/L16 8k/16k)
    - Background audio mixing
    """

    def __init__(self, endpoint, session_id: str, transport: "AudioStreamTransport",
                 params: Optional[TransportParams] = None, **kwargs):
        if params is None:
            params = TransportParams(
                audio_out_enabled=True,
            )
        super().__init__(params, **kwargs)
        self._ep = endpoint
        self._sid = session_id
        self._transport = transport
        self._started = False

    async def start(self, frame: StartFrame):
        if self._started:
            return
        self._started = True
        await super().start(frame)
        await self.set_transport_ready(frame)

    def _supports_native_dtmf(self) -> bool:
        return True

    async def _write_dtmf_native(self, frame):
        digit = str(frame.button.value)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self._ep.send_dtmf(self._sid, digit))

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        """Send audio frame to Plivo via Rust endpoint.

        The Rust endpoint handles:
        - Buffering in AudioBuffer with backpressure
        - Resampling 16kHz → 8kHz if needed
        - Encoding to negotiated format (mulaw/L16)
        - Base64 encoding and JSON wrapping
        - Paced sending at 20ms intervals
        """
        try:
            self._ep.send_audio_bytes(self._sid, frame.audio, frame.sample_rate, frame.num_channels)
            return True
        except Exception as e:
            logger.error("write_audio_frame failed: %s", e)
            return False

    def queued_frames(self) -> int:
        """Number of 20ms audio frames buffered in the Rust outgoing queue.

        Multiply by 0.02 to get seconds of buffered audio.
        """
        try:
            return self._ep.queued_frames(self._sid)
        except Exception:
            return 0

    async def send_message(self, frame):
        """Send OutputTransportMessageFrame as raw JSON over the WebSocket.

        Filters RTVI internal messages (matching PlivoFrameSerializer.should_ignore_frame).
        Non-dict messages are serialized to JSON string.
        """
        if isinstance(frame.message, dict) and frame.message.get("label") == "rtvi-ai":
            return
        try:
            msg = json.dumps(frame.message) if not isinstance(frame.message, str) else frame.message
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: self._ep.send_raw_message(self._sid, msg))
        except Exception as e:
            logger.warning("send_message failed: %s", e)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Handle InterruptionFrame → clearAudio before base class processing.

        Sends clearAudio to Plivo (clears server-side buffer) and clears the
        local AudioBuffer (fires pending completion callbacks). The base class
        then cancels and restarts the MediaSender audio task.
        """
        if isinstance(frame, InterruptionFrame):
            try:
                self._ep.clear_buffer(self._sid)
            except Exception as e:
                logger.debug("clear_buffer on interruption failed: %s", e)

        await super().process_frame(frame, direction)

    async def stop(self, frame: EndFrame):
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: self._ep.hangup(self._sid))
        except Exception as e:
            logger.debug("hangup on stop failed: %s", e)
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: self._ep.hangup(self._sid))
        except Exception as e:
            logger.debug("hangup on cancel failed: %s", e)
        await super().cancel(frame)


# ─── Composite Transport ────────────────────────────────────────────────────


class AudioStreamTransport(BaseTransport):
    """Pipecat transport for Plivo audio streaming via agent-transport.

    This is the main entry point. It creates lazily-initialized input and
    output transport processors that are wired into a Pipecat pipeline.

    Architecture:
        AudioStreamEndpoint (Rust) handles the WebSocket server, audio codec
        negotiation, 20ms paced sending, and all Plivo protocol messages.
        This transport bridges Pipecat frames to the Rust endpoint.

    Event handlers (register via @transport.event_handler("name")):
        on_client_connected  — Plivo WebSocket connected, stream started
        on_client_disconnected — stream stopped or WebSocket disconnected
        on_beep_detected — voicemail beep detected (if beep detection active)
        on_beep_timeout — beep detection timed out

    Session metadata (from Plivo start event):
        transport.session_id    — internal session ID
        transport.call_id       — Plivo call UUID
        transport.stream_id     — Plivo stream UUID
        transport.extra_headers — custom headers from <Stream> element

    Plivo feature methods:
        mute() / unmute()           — mute/unmute outgoing audio
        pause_playback() / resume_playback() — pause/resume audio send loop
        start_recording() / stop_recording() — OGG/Opus call recording
        clear_buffer()              — clear audio + send clearAudio to Plivo
        send_background_audio()     — mix background audio with agent voice
        checkpoint() / wait_for_playout() — track playback completion
    """

    def __init__(
        self,
        endpoint,
        session_id: str,
        *,
        name: Optional[str] = None,
        params: Optional[TransportParams] = None,
        session_data: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        super().__init__(name=name or "AudioStreamTransport", **kwargs)
        self._ep = endpoint
        self._sid = session_id
        self._params = params
        self._session_data = session_data or {}
        self._input: Optional[AudioStreamInputTransport] = None
        self._output: Optional[AudioStreamOutputTransport] = None

        # Register Pipecat event handlers (same pattern as WebsocketServerTransport)
        self._register_event_handler("on_client_connected")
        self._register_event_handler("on_client_disconnected")
        self._register_event_handler("on_beep_detected")
        self._register_event_handler("on_beep_timeout")

    # ── Pipeline processors ──────────────────────────────────────────────

    def input(self) -> AudioStreamInputTransport:
        if self._input is None:
            self._input = AudioStreamInputTransport(
                self._ep, self._sid, transport=self,
                params=self._params, name=f"{self._name}-input",
            )
        return self._input

    def output(self) -> AudioStreamOutputTransport:
        if self._output is None:
            self._output = AudioStreamOutputTransport(
                self._ep, self._sid, transport=self,
                params=self._params, name=f"{self._name}-output",
            )
        return self._output

    # ── Session metadata (from Plivo start event) ────────────────────────

    @property
    def session_id(self) -> str:
        """Internal session ID used to address this stream."""
        return self._sid

    @property
    def call_id(self) -> str:
        """Plivo call UUID (from start event). Used for REST API operations."""
        return self._session_data.get("call_uuid", "")

    @property
    def stream_id(self) -> str:
        """Plivo stream UUID (from start event)."""
        return self._session_data.get("local_uri", "")

    @property
    def extra_headers(self) -> Dict[str, str]:
        """Custom headers from <Stream> element's extraHeaders attribute."""
        return self._session_data.get("extra_headers", {})

    # ── Plivo audio control ──────────────────────────────────────────────

    def mute(self) -> None:
        """Mute outgoing audio. Send loop outputs silence, queue preserved."""
        self._ep.mute(self._sid)

    def unmute(self) -> None:
        """Unmute outgoing audio."""
        self._ep.unmute(self._sid)

    def pause_playback(self) -> None:
        """Pause the audio send loop. Queue accumulates, nothing sent to Plivo."""
        self._ep.pause(self._sid)

    def resume_playback(self) -> None:
        """Resume the audio send loop."""
        self._ep.resume(self._sid)

    def clear_buffer(self) -> None:
        """Clear local audio buffer AND send clearAudio to Plivo.

        Two-level clear:
        1. Local AudioBuffer cleared (fires pending completion callbacks)
        2. clearAudio sent to Plivo (clears server-side playback buffer)
        """
        self._ep.clear_buffer(self._sid)

    # ── Background audio ─────────────────────────────────────────────────

    def send_background_audio(self, audio: bytes, sample_rate: int, num_channels: int) -> None:
        """Send background audio to be mixed with agent voice.

        Background audio is mixed in the Rust send loop at 20ms intervals.
        No backpressure — background audio is best-effort.

        Use cases: hold music, ambient sounds, notification tones.
        """
        self._ep.send_background_audio(self._sid, audio, sample_rate, num_channels)

    # ── Checkpoint / playout tracking ────────────────────────────────────

    def checkpoint(self, name: Optional[str] = None) -> str:
        """Send a checkpoint to Plivo.

        Plivo responds with a playedStream event when all audio queued
        before this checkpoint has been played to the caller.

        Args:
            name: Optional checkpoint name. Auto-generated if not provided.

        Returns:
            The checkpoint name (for tracking).
        """
        return self._ep.checkpoint(self._sid, name)

    async def wait_for_playout(self, timeout_ms: int = 5000) -> bool:
        """Wait for the last checkpoint to be confirmed by Plivo.

        Blocks until Plivo sends a playedStream event confirming that all
        audio up to the last checkpoint has been played to the caller.

        Args:
            timeout_ms: Maximum time to wait in milliseconds.

        Returns:
            True if playout confirmed, False if timed out.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self._ep.wait_for_playout(self._sid, timeout_ms)
        )

    # ── Recording ────────────────────────────────────────────────────────

    def start_recording(self, path: str, stereo: bool = True) -> None:
        """Start recording the call to an OGG/Opus file.

        Args:
            path: Output file path (.ogg).
            stereo: If True, record stereo (L=user, R=agent). Otherwise mono.
        """
        self._ep.start_recording(self._sid, path, stereo)

    def stop_recording(self) -> None:
        """Stop recording the call."""
        self._ep.stop_recording(self._sid)

    # ── DTMF ─────────────────────────────────────────────────────────────

    def send_dtmf(self, digits: str) -> None:
        """Send DTMF digits to the caller via Plivo.

        Args:
            digits: One or more DTMF digits (0-9, *, #, A-D).
        """
        self._ep.send_dtmf(self._sid, digits)

    # ── Raw WebSocket access ─────────────────────────────────────────────

    def send_raw_message(self, message: str) -> None:
        """Send a raw JSON message over the Plivo WebSocket.

        For advanced use cases where you need to send custom messages.
        """
        self._ep.send_raw_message(self._sid, message)
