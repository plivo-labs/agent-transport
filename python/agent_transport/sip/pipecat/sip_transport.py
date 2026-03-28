"""Pipecat BaseTransport adapter for SIP transport.

Replaces Pipecat's WebsocketServerTransport for SIP calls.
All audio codec/resampling/pacing is handled in Rust. Python only bridges frames.

Uses Pipecat's MediaSender infrastructure (via set_transport_ready) for:
- Audio chunking
- BotStartedSpeaking/BotStoppedSpeaking state management
- Proper interruption handling

Usage:
    from agent_transport import SipEndpoint
    from agent_transport.sip.pipecat import SipTransport

    ep = SipEndpoint(sip_server="phone.plivo.com")
    ep.register(username, password)
    call_id = ep.call(dest_uri)

    transport = SipTransport(ep, call_id)
    pipeline = Pipeline([transport.input(), stt, llm, tts, transport.output()])
"""

import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from pipecat.audio.dtmf.types import KeypadEntry
    from pipecat.frames.frames import (
        CancelFrame, EndFrame, Frame, InputAudioRawFrame,
        InputDTMFFrame, InterruptionFrame, OutputAudioRawFrame,
        StartFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection
    from pipecat.transports.base_input import BaseInputTransport
    from pipecat.transports.base_output import BaseOutputTransport
    from pipecat.transports.base_transport import BaseTransport, TransportParams
except ImportError:
    raise ImportError("pipecat-ai is required: pip install pipecat-ai")


class SipInputTransport(BaseInputTransport):
    """Receives audio from SIP call. Uses Rust blocking recv (GIL released)."""

    def __init__(self, endpoint, call_id: str, params: Optional[TransportParams] = None, **kwargs):
        if params is None:
            params = TransportParams(
                audio_in_enabled=True,
                audio_in_passthrough=True,
            )
        super().__init__(params, **kwargs)
        self._ep = endpoint
        self._cid = call_id
        self._running = False
        self._recv_task = None
        self._dtmf_task = None

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self._running = True
        await self.set_transport_ready(frame)
        self._recv_task = asyncio.create_task(self._audio_recv_loop())
        self._dtmf_task = asyncio.create_task(self._event_recv_loop())

    async def stop(self, frame: EndFrame):
        self._running = False
        if self._recv_task: self._recv_task.cancel()
        if self._dtmf_task: self._dtmf_task.cancel()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        self._running = False
        if self._recv_task: self._recv_task.cancel()
        if self._dtmf_task: self._dtmf_task.cancel()
        await super().cancel(frame)

    async def _audio_recv_loop(self):
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                result = await loop.run_in_executor(
                    None, lambda: self._ep.recv_audio_bytes_blocking(self._cid, 20)
                )
            except Exception:
                break  # Session removed — exit cleanly
            if result is not None:
                audio_bytes, sample_rate, num_channels = result
                await self.push_audio_frame(InputAudioRawFrame(
                    audio=bytes(audio_bytes), sample_rate=sample_rate, num_channels=num_channels,
                ))

    async def _event_recv_loop(self):
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
            if event["type"] == "dtmf_received":
                await self.push_frame(InputDTMFFrame(button=KeypadEntry(event["digit"])))
            elif event["type"] == "call_terminated":
                self._running = False
                await self.push_frame(EndFrame())


class SipOutputTransport(BaseOutputTransport):
    """Sends audio to SIP call.

    Uses Pipecat's MediaSender infrastructure for audio chunking,
    bot speaking state, and interruption handling.
    """

    def __init__(self, endpoint, call_id: str, params: Optional[TransportParams] = None, **kwargs):
        if params is None:
            params = TransportParams(
                audio_out_enabled=True,
            )
        super().__init__(params, **kwargs)
        self._ep = endpoint
        self._cid = call_id

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        try:
            self._ep.send_audio_bytes(self._cid, frame.audio, frame.sample_rate, frame.num_channels)
            return True
        except Exception as e:
            logger.error("write_audio_frame failed: %s", e)
            return False

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

    def _supports_native_dtmf(self) -> bool:
        return True

    async def _write_dtmf_native(self, frame):
        digit = str(frame.button.value)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self._ep.send_dtmf(self._cid, digit))

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Handle InterruptionFrame → clear_buffer before base class processing."""
        if isinstance(frame, InterruptionFrame):
            try: self._ep.clear_buffer(self._cid)
            except Exception as e: logger.debug("clear_buffer on interruption failed: %s", e)

        await super().process_frame(frame, direction)

    async def stop(self, frame: EndFrame):
        loop = asyncio.get_running_loop()
        try: await loop.run_in_executor(None, lambda: self._ep.hangup(self._cid))
        except Exception as e: logger.debug("hangup on stop failed: %s", e)
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        loop = asyncio.get_running_loop()
        try: await loop.run_in_executor(None, lambda: self._ep.hangup(self._cid))
        except Exception as e: logger.debug("hangup on cancel failed: %s", e)
        await super().cancel(frame)


class SipTransport(BaseTransport):
    """Pipecat transport for SIP calls via agent-transport."""

    def __init__(self, endpoint, call_id: str, *, name: Optional[str] = None,
                 params: Optional[TransportParams] = None, **kwargs):
        super().__init__(name=name or "SipTransport", **kwargs)
        self._ep = endpoint
        self._cid = call_id
        self._params = params
        self._input = None
        self._output = None

    def input(self) -> SipInputTransport:
        if self._input is None:
            self._input = SipInputTransport(
                self._ep, self._cid, params=self._params, name=f"{self._name}-input",
            )
        return self._input

    def output(self) -> SipOutputTransport:
        if self._output is None:
            self._output = SipOutputTransport(
                self._ep, self._cid, params=self._params, name=f"{self._name}-output",
            )
        return self._output
