"""LiveKit Agents AudioInput/AudioOutput adapters for Plivo audio streaming.

Uses raw bytes API for zero-copy PCM transfer.
"""

import asyncio
from typing import Optional

try:
    from livekit import rtc
    from livekit.agents.voice.io import AudioInput, AudioOutput
except ImportError:
    raise ImportError("livekit-agents is required: pip install livekit-agents")

from .sip_io import _to_livekit_frame


class AudioStreamInput(AudioInput):
    """Async iterator yielding AudioFrames from Plivo audio stream."""

    def __init__(self, endpoint, session_id: int):
        self._ep = endpoint
        self._sid = session_id
        self._closed = False

    async def __anext__(self) -> rtc.AudioFrame:
        loop = asyncio.get_event_loop()
        while not self._closed:
            result = await loop.run_in_executor(
                None, lambda: self._ep.recv_audio_bytes_blocking(self._sid, 20)
            )
            if result is not None:
                audio_bytes, sample_rate, num_channels = result
                return _to_livekit_frame(bytes(audio_bytes), sample_rate, num_channels)
        raise StopAsyncIteration

    def __aiter__(self):
        return self

    def on_attached(self) -> None:
        pass

    def on_detached(self) -> None:
        self._closed = True


class AudioStreamOutput(AudioOutput):
    """Sends AudioFrames to Plivo audio stream."""

    def __init__(self, endpoint, session_id: int):
        super().__init__()
        self._ep = endpoint
        self._sid = session_id

    @property
    def sample_rate(self) -> Optional[int]:
        return 16000

    @property
    def can_pause(self) -> bool:
        return False

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        self._ep.send_audio_bytes(self._sid, bytes(frame.data), frame.sample_rate, frame.num_channels)

    def flush(self) -> None:
        pass  # Audio streaming is fire-and-forget

    def clear_buffer(self) -> None:
        self._ep.clear_buffer(self._sid)

    def pause(self) -> None:
        pass

    def resume(self) -> None:
        pass

    def on_attached(self) -> None:
        pass

    def on_detached(self) -> None:
        pass
