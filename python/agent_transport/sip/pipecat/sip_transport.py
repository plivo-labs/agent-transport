"""Pipecat BaseTransport adapter for SIP transport.

Replaces Pipecat's WebsocketServerTransport for SIP calls.
All audio codec/resampling/pacing is handled in Rust. Python only bridges frames.

100% compatible with Pipecat's transport interface. Exposes all SIP features:
- Event handlers: on_client_connected, on_client_disconnected, on_beep_detected, on_beep_timeout
- Session metadata: session_id, call_id, remote_uri, direction, extra_headers
- SIP call control: transfer, hold, reject, beep detection
- Audio: mute, recording, background audio, flush/playout

Frame handling:
- OutputAudioRawFrame → send_audio_bytes (Rust encodes + paces at 20ms via RTP)
- InterruptionFrame → clear_buffer
- OutputDTMFFrame → send_dtmf (RFC 2833 or SIP INFO)
- OutputTransportMessageFrame → send_info (SIP INFO with JSON body)
- EndFrame/CancelFrame → hangup
- InputAudioRawFrame ← recv_audio_bytes_blocking (Rust decodes + resamples)
- InputDTMFFrame ← event polling

Bot speaking state (BotStartedSpeaking/BotStoppedSpeaking) is handled by
Pipecat's BaseOutputTransport MediaSender infrastructure.
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


class SipInputTransport(BaseInputTransport):
    """Receives audio + DTMF from SIP call.

    Audio is received from the Rust endpoint via blocking recv (GIL released),
    decoded and resampled in Rust, and pushed as InputAudioRawFrame into the
    Pipecat pipeline. DTMF and call lifecycle events are polled separately.
    """

    def __init__(self, endpoint, call_id: str, transport: "SipTransport",
                 params: Optional[TransportParams] = None,
                 event_queue: Optional[asyncio.Queue] = None, **kwargs):
        if params is None:
            params = TransportParams(
                audio_in_enabled=True,
                audio_in_passthrough=True,
            )
        super().__init__(params, **kwargs)
        self._ep = endpoint
        self._cid = call_id
        self._transport = transport
        self._event_queue = event_queue
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
        await self._transport._call_event_handler("on_client_connected")

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
                    None, lambda: self._ep.recv_audio_bytes_blocking(self._cid, 20)
                )
            except Exception as e:
                logger.debug("SipInputTransport recv_audio error: %s", e)
                break
            if result is not None:
                audio_bytes, sample_rate, num_channels = result
                await self.push_audio_frame(InputAudioRawFrame(
                    audio=bytes(audio_bytes), sample_rate=sample_rate, num_channels=num_channels,
                ))

    async def _event_loop(self):
        """Poll for DTMF, call state, and lifecycle events.

        If an event_queue is provided (by the server), reads from it.
        Otherwise falls back to polling endpoint directly (standalone usage).
        """
        if self._event_queue:
            await self._event_loop_from_queue()
        else:
            await self._event_loop_from_endpoint()

    async def _event_loop_from_queue(self):
        """Read events from per-session queue (dispatched by server)."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.debug("SipInputTransport event_loop error: %s", e)
                break
            await self._handle_event(event)

    async def _event_loop_from_endpoint(self):
        """Poll events directly from endpoint (standalone, no server)."""
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                event = await loop.run_in_executor(
                    None, lambda: self._ep.wait_for_event(timeout_ms=100)
                )
            except Exception as e:
                logger.debug("SipInputTransport endpoint event_loop error: %s", e)
                break
            if event is None:
                continue
            await self._handle_event(event)

    async def _handle_event(self, event):
        """Process a single event."""
        event_type = event.get("type", "")
        if event_type == "dtmf_received":
            await self.push_frame(InputDTMFFrame(button=KeypadEntry(event["digit"])))
        elif event_type == "call_terminated":
            self._running = False
            await self._transport._call_event_handler("on_client_disconnected")
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


class SipOutputTransport(BaseOutputTransport):
    """Sends audio to SIP call.

    Uses Pipecat's MediaSender infrastructure (via set_transport_ready) for:
    - Audio chunking to transport chunk size
    - BotStartedSpeaking/BotStoppedSpeaking state management
    - Proper interruption handling (task cancellation + restart)

    Audio is sent to the Rust endpoint which handles:
    - RTP packetization and 20ms pacing
    - G.711 codec encoding
    - Jitter buffer, PLC, comfort noise (if enabled)
    """

    def __init__(self, endpoint, call_id: str, transport: "SipTransport",
                 params: Optional[TransportParams] = None, **kwargs):
        if params is None:
            params = TransportParams(
                audio_out_enabled=True,
            )
        super().__init__(params, **kwargs)
        self._ep = endpoint
        self._cid = call_id
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
        await loop.run_in_executor(None, lambda: self._ep.send_dtmf(self._cid, digit))

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        """Send audio frame to SIP call via Rust endpoint with backpressure."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        def _on_complete():
            try:
                loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(None))
            except RuntimeError:
                try:
                    fut.set_result(None)
                except Exception:
                    pass

        try:
            self._ep.send_audio_notify(
                self._cid, frame.audio, frame.sample_rate, frame.num_channels, _on_complete
            )
            await fut
            return True
        except Exception as e:
            logger.error("write_audio_frame failed: %s", e)
            return False

    def queued_frames(self) -> int:
        """Number of 20ms audio frames buffered in the Rust outgoing queue."""
        try:
            return self._ep.queued_frames(self._cid)
        except Exception:
            return 0

    async def send_message(self, frame):
        """Send OutputTransportMessageFrame as SIP INFO with JSON body.

        Filters RTVI internal messages (matching should_ignore_frame).
        """
        if isinstance(frame.message, dict) and frame.message.get("label") == "rtvi-ai":
            return
        try:
            msg = json.dumps(frame.message) if not isinstance(frame.message, str) else frame.message
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: self._ep.send_info(self._cid, "application/json", msg)
            )
        except Exception as e:
            logger.warning("send_message via SIP INFO failed: %s", e)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Handle InterruptionFrame → clear_buffer before base class processing."""
        if isinstance(frame, InterruptionFrame):
            try:
                self._ep.clear_buffer(self._cid)
            except Exception as e:
                logger.debug("clear_buffer on interruption failed: %s", e)

        await super().process_frame(frame, direction)

    async def stop(self, frame: EndFrame):
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: self._ep.hangup(self._cid))
        except Exception as e:
            logger.debug("hangup on stop failed: %s", e)
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: self._ep.hangup(self._cid))
        except Exception as e:
            logger.debug("hangup on cancel failed: %s", e)
        await super().cancel(frame)


# ─── Composite Transport ────────────────────────────────────────────────────


class SipTransport(BaseTransport):
    """Pipecat transport for SIP calls via agent-transport.

    This is the main entry point. It creates lazily-initialized input and
    output transport processors that are wired into a Pipecat pipeline.

    Architecture:
        SipEndpoint (Rust) handles SIP signaling, RTP audio, codec encoding,
        and 20ms pacing. This transport bridges Pipecat frames to the Rust endpoint.

    Event handlers (register via @transport.event_handler("name")):
        on_client_connected  — SIP call answered, media active
        on_client_disconnected — call terminated (BYE or hangup)
        on_beep_detected — voicemail beep detected
        on_beep_timeout — beep detection timed out

    Session metadata (from SIP call session):
        transport.session_id    — internal call ID
        transport.call_id       — SIP call UUID
        transport.remote_uri    — remote SIP URI
        transport.direction     — "Inbound" or "Outbound"
        transport.extra_headers — custom SIP headers

    Audio control:
        mute() / unmute()           — mute/unmute outgoing audio
        pause_playback() / resume_playback() — pause/resume RTP send loop
        clear_buffer()              — clear queued audio
        send_background_audio()     — mix background audio with agent voice
        start_recording() / stop_recording() — WAV stereo call recording
        flush() / wait_for_playout() — track playback completion

    SIP-specific:
        transfer() / transfer_attended() — call transfer
        hold() / unhold() — SIP hold (re-INVITE)
        reject() — reject incoming call
        detect_beep() / cancel_beep_detection() — voicemail detection
        send_dtmf() — send DTMF digits
        send_info() — send SIP INFO message
    """

    def __init__(
        self,
        endpoint,
        call_id: str,
        *,
        name: Optional[str] = None,
        params: Optional[TransportParams] = None,
        session_data: Optional[Dict[str, Any]] = None,
        _event_queue: Optional[asyncio.Queue] = None,
        **kwargs,
    ):
        super().__init__(name=name or "SipTransport", **kwargs)
        self._ep = endpoint
        self._cid = call_id
        self._params = params
        self._session_data = session_data or {}
        self._event_queue = _event_queue
        self._input: Optional[SipInputTransport] = None
        self._output: Optional[SipOutputTransport] = None

        # Register Pipecat event handlers
        self._register_event_handler("on_client_connected")
        self._register_event_handler("on_client_disconnected")
        self._register_event_handler("on_beep_detected")
        self._register_event_handler("on_beep_timeout")

    # ── Pipeline processors ──────────────────────────────────────────────

    def input(self) -> SipInputTransport:
        if self._input is None:
            self._input = SipInputTransport(
                self._ep, self._cid, transport=self,
                params=self._params, event_queue=self._event_queue,
                name=f"{self._name}-input",
            )
        return self._input

    def output(self) -> SipOutputTransport:
        if self._output is None:
            self._output = SipOutputTransport(
                self._ep, self._cid, transport=self,
                params=self._params, name=f"{self._name}-output",
            )
        return self._output

    # ── Session metadata ─────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        """Internal call ID used to address this session."""
        return self._cid

    @property
    def call_id(self) -> str:
        """SIP call UUID."""
        return self._session_data.get("call_uuid", self._cid)

    @property
    def remote_uri(self) -> str:
        """Remote SIP URI (e.g., sip:+1234567890@provider.com)."""
        return self._session_data.get("remote_uri", "")

    @property
    def direction(self) -> str:
        """Call direction: 'Inbound' or 'Outbound'."""
        return self._session_data.get("direction", "")

    @property
    def extra_headers(self) -> Dict[str, str]:
        """Custom SIP headers from the INVITE."""
        return self._session_data.get("extra_headers", {})

    # ── Audio control ────────────────────────────────────────────────────

    def mute(self) -> None:
        """Mute outgoing audio. RTP sends silence, queue preserved."""
        self._ep.mute(self._cid)

    def unmute(self) -> None:
        """Unmute outgoing audio."""
        self._ep.unmute(self._cid)

    def pause_playback(self) -> None:
        """Pause the RTP send loop. Queue accumulates, nothing sent."""
        self._ep.pause(self._cid)

    def resume_playback(self) -> None:
        """Resume the RTP send loop."""
        self._ep.resume(self._cid)

    def clear_buffer(self) -> None:
        """Clear local audio buffer (fires pending completion callbacks)."""
        self._ep.clear_buffer(self._cid)

    # ── Background audio ─────────────────────────────────────────────────

    def send_background_audio(self, audio: bytes, sample_rate: int, num_channels: int) -> None:
        """Send background audio to be mixed with agent voice in Rust's RTP send loop."""
        self._ep.send_background_audio(self._cid, audio, sample_rate, num_channels)

    # ── Flush / playout tracking ─────────────────────────────────────────

    def flush(self) -> None:
        """Mark current playback segment complete."""
        self._ep.flush(self._cid)

    async def wait_for_playout(self, timeout_ms: int = 5000) -> bool:
        """Wait for queued audio to finish playing.

        Returns True if playout completed, False if timed out.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self._ep.wait_for_playout(self._cid, timeout_ms)
        )

    # ── Recording ────────────────────────────────────────────────────────

    def start_recording(self, path: str, stereo: bool = True) -> None:
        """Start recording the call to a WAV file.

        Args:
            path: Output file path (.wav).
            stereo: If True, record stereo (L=user, R=agent). Otherwise mono.
        """
        self._ep.start_recording(self._cid, path, stereo)

    def stop_recording(self) -> None:
        """Stop recording the call."""
        self._ep.stop_recording(self._cid)

    # ── DTMF ─────────────────────────────────────────────────────────────

    def send_dtmf(self, digits: str, method: str = "rfc2833") -> None:
        """Send DTMF digits.

        Args:
            digits: One or more DTMF digits (0-9, *, #, A-D).
            method: 'rfc2833' (in-band RTP) or 'sip_info' (SIP INFO).
        """
        self._ep.send_dtmf(self._cid, digits, method)

    # ── SIP call control ─────────────────────────────────────────────────

    def transfer(self, dest_uri: str) -> None:
        """Blind transfer — transfer call to another SIP URI."""
        self._ep.transfer(self._cid, dest_uri)

    def transfer_attended(self, target_call_id: str) -> None:
        """Attended transfer — transfer to an existing call."""
        self._ep.transfer_attended(self._cid, target_call_id)

    def hold(self) -> None:
        """Put the call on hold (SIP re-INVITE with a=sendonly)."""
        self._ep.hold(self._cid)

    def unhold(self) -> None:
        """Take the call off hold (SIP re-INVITE with a=sendrecv)."""
        self._ep.unhold(self._cid)

    def reject(self, code: int = 486) -> None:
        """Reject an incoming call with a SIP status code."""
        self._ep.reject(self._cid, code)

    def send_info(self, content_type: str = "application/json", body: str = "") -> None:
        """Send a SIP INFO message."""
        self._ep.send_info(self._cid, content_type, body)

    # ── Beep detection ───────────────────────────────────────────────────

    def detect_beep(self, timeout_ms: int = 30000, min_duration_ms: int = 80,
                    max_duration_ms: int = 5000) -> None:
        """Start voicemail beep detection.

        Fires on_beep_detected or on_beep_timeout event when done.
        """
        self._ep.detect_beep(self._cid, timeout_ms, min_duration_ms, max_duration_ms)

    def cancel_beep_detection(self) -> None:
        """Cancel ongoing beep detection."""
        self._ep.cancel_beep_detection(self._cid)

    # ── Raw message ──────────────────────────────────────────────────────

    def send_raw_message(self, content_type: str, body: str) -> None:
        """Send a raw SIP INFO message."""
        self._ep.send_info(self._cid, content_type, body)
