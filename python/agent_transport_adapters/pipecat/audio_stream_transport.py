"""Pipecat BaseTransport adapter for Plivo audio streaming.

Uses raw bytes API for zero-copy PCM transfer.

Usage:
    from agent_transport_adapters.pipecat import AudioStreamTransport
    transport = AudioStreamTransport(endpoint, session_id)
    pipeline = Pipeline([transport.input(), stt, llm, tts, transport.output()])
"""

import asyncio
from typing import Optional

try:
    from pipecat.frames.frames import (
        CancelFrame, EndFrame, InputAudioRawFrame,
        OutputAudioRawFrame, StartFrame,
    )
    from pipecat.transports.base_input import BaseInputTransport
    from pipecat.transports.base_output import BaseOutputTransport
    from pipecat.transports.base_transport import BaseTransport
except ImportError:
    raise ImportError("pipecat-ai is required: pip install pipecat-ai")


class AudioStreamInputTransport(BaseInputTransport):
    """Receives audio from Plivo audio stream. Blocking recv in Rust thread."""

    def __init__(self, endpoint, session_id: int, **kwargs):
        super().__init__(**kwargs)
        self._ep = endpoint
        self._sid = session_id
        self._running = False
        self._task = None

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self._running = True
        self._task = asyncio.create_task(self._recv_loop())

    async def stop(self, frame: EndFrame):
        self._running = False
        if self._task: self._task.cancel()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        self._running = False
        if self._task: self._task.cancel()
        await super().cancel(frame)

    async def _recv_loop(self):
        loop = asyncio.get_event_loop()
        while self._running:
            result = await loop.run_in_executor(
                None, lambda: self._ep.recv_audio_bytes_blocking(self._sid, 20)
            )
            if result is not None:
                audio_bytes, sample_rate, num_channels = result
                await self.push_audio_frame(InputAudioRawFrame(
                    audio=bytes(audio_bytes), sample_rate=sample_rate, num_channels=num_channels,
                ))


class AudioStreamOutputTransport(BaseOutputTransport):
    """Sends audio to Plivo audio stream. Raw bytes, no conversion."""

    def __init__(self, endpoint, session_id: int, **kwargs):
        super().__init__(**kwargs)
        self._ep = endpoint
        self._sid = session_id

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        try:
            self._ep.send_audio_bytes(self._sid, frame.audio, frame.sample_rate, frame.num_channels)
            return True
        except Exception:
            return False

    async def stop(self, frame: EndFrame):
        try: self._ep.hangup(self._sid)
        except Exception: pass
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        try: self._ep.hangup(self._sid)
        except Exception: pass
        await super().cancel(frame)


class AudioStreamTransport(BaseTransport):
    """Pipecat transport for Plivo audio streaming."""

    def __init__(self, endpoint, session_id: int, *, name: Optional[str] = None, **kwargs):
        super().__init__(name=name or "AudioStreamTransport", **kwargs)
        self._ep = endpoint
        self._sid = session_id
        self._input = None
        self._output = None

    def input(self) -> AudioStreamInputTransport:
        if self._input is None:
            self._input = AudioStreamInputTransport(self._ep, self._sid, name=f"{self._name}-input")
        return self._input

    def output(self) -> AudioStreamOutputTransport:
        if self._output is None:
            self._output = AudioStreamOutputTransport(self._ep, self._sid, name=f"{self._name}-output")
        return self._output
