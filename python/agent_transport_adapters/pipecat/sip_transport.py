"""Pipecat BaseTransport adapter for SIP transport.

Uses recv_audio_blocking() via run_in_executor to avoid Python polling loops.
This prevents audio jitter at high concurrency.

Usage:
    from agent_transport import SipEndpoint
    from agent_transport_adapters.pipecat import SipTransport

    ep = SipEndpoint(sip_server="phone.plivo.com")
    ep.register(username, password)
    call_id = ep.call(dest_uri)

    transport = SipTransport(ep, call_id)
    pipeline = Pipeline([transport.input(), stt, llm, tts, transport.output()])
"""

import asyncio
import struct
from typing import Optional

try:
    from pipecat.frames.frames import (
        CancelFrame, EndFrame, Frame, InputAudioRawFrame,
        InputDTMFFrame, OutputAudioRawFrame, StartFrame,
    )
    from pipecat.transports.base_input import BaseInputTransport
    from pipecat.transports.base_output import BaseOutputTransport
    from pipecat.transports.base_transport import BaseTransport
except ImportError:
    raise ImportError("pipecat-ai is required: pip install pipecat-ai")

from agent_transport import SipEndpoint, AudioFrame


class SipInputTransport(BaseInputTransport):
    """Receives audio from SIP call and pushes to Pipecat pipeline.

    Uses Rust-side blocking recv (GIL released) instead of Python polling.
    """

    def __init__(self, endpoint: SipEndpoint, call_id: int, **kwargs):
        super().__init__(**kwargs)
        self._ep = endpoint
        self._cid = call_id
        self._running = False
        self._audio_task = None
        self._event_task = None

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self._running = True
        self._audio_task = asyncio.create_task(self._audio_recv_loop())
        self._event_task = asyncio.create_task(self._event_recv_loop())

    async def stop(self, frame: EndFrame):
        self._running = False
        if self._audio_task: self._audio_task.cancel()
        if self._event_task: self._event_task.cancel()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        self._running = False
        if self._audio_task: self._audio_task.cancel()
        if self._event_task: self._event_task.cancel()
        await super().cancel(frame)

    async def _audio_recv_loop(self):
        """Receive audio via blocking Rust call in thread pool."""
        loop = asyncio.get_event_loop()
        while self._running:
            # Block in Rust thread, not Python event loop
            frame = await loop.run_in_executor(
                None, lambda: self._ep.recv_audio_blocking(self._cid, 20)
            )
            if frame is not None:
                pcm_bytes = struct.pack(f"<{len(frame.data)}h", *frame.data)
                await self.push_audio_frame(InputAudioRawFrame(
                    audio=pcm_bytes, sample_rate=frame.sample_rate, num_channels=frame.num_channels,
                ))

    async def _event_recv_loop(self):
        """Receive events via blocking Rust call in thread pool."""
        loop = asyncio.get_event_loop()
        while self._running:
            event = await loop.run_in_executor(
                None, lambda: self._ep.wait_for_event(timeout_ms=100)
            )
            if event is None:
                continue
            if event["type"] == "dtmf_received":
                await self.push_frame(InputDTMFFrame(digit=event["digit"]))
            elif event["type"] == "call_terminated":
                self._running = False
                await self.push_frame(EndFrame())


class SipOutputTransport(BaseOutputTransport):
    """Sends audio to SIP call from Pipecat pipeline."""

    def __init__(self, endpoint: SipEndpoint, call_id: int, **kwargs):
        super().__init__(**kwargs)
        self._ep = endpoint
        self._cid = call_id

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        n_samples = len(frame.audio) // 2
        data = list(struct.unpack(f"<{n_samples}h", frame.audio))
        try:
            self._ep.send_audio(self._cid, AudioFrame(data, frame.sample_rate, frame.num_channels))
            return True
        except Exception:
            return False

    def _supports_native_dtmf(self) -> bool:
        return True

    async def _write_dtmf_native(self, frame):
        digit = str(frame.digit) if hasattr(frame, "digit") else str(frame)
        self._ep.send_dtmf(self._cid, digit)

    async def stop(self, frame: EndFrame):
        try: self._ep.hangup(self._cid)
        except Exception: pass
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        try: self._ep.hangup(self._cid)
        except Exception: pass
        await super().cancel(frame)


class SipTransport(BaseTransport):
    """Pipecat transport for SIP calls via agent-transport."""

    def __init__(self, endpoint: SipEndpoint, call_id: int, *, name: Optional[str] = None, **kwargs):
        super().__init__(name=name or "SipTransport", **kwargs)
        self._ep = endpoint
        self._cid = call_id
        self._input = None
        self._output = None

    def input(self) -> SipInputTransport:
        if self._input is None:
            self._input = SipInputTransport(self._ep, self._cid, name=f"{self._name}-input")
        return self._input

    def output(self) -> SipOutputTransport:
        if self._output is None:
            self._output = SipOutputTransport(self._ep, self._cid, name=f"{self._name}-output")
        return self._output
