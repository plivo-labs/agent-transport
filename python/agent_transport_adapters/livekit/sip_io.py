"""LiveKit Agents AudioInput/AudioOutput adapters for SIP transport.

Uses recv_audio_bytes_blocking() for zero-copy PCM in the input path.
Uses send_audio_bytes() for zero-copy PCM in the output path.

Usage:
    from agent_transport import SipEndpoint
    from agent_transport_adapters.livekit import SipAudioInput, SipAudioOutput

    ep = SipEndpoint(sip_server="phone.plivo.com")
    ep.register(username, password)
    call_id = ep.call(dest_uri)

    session.input.audio = SipAudioInput(ep, call_id)
    session.output.audio = SipAudioOutput(ep, call_id)
"""

import asyncio
from typing import Optional

try:
    from livekit import rtc
    from livekit.agents.voice.io import AudioInput, AudioOutput
except ImportError:
    raise ImportError("livekit-agents is required: pip install livekit-agents")


def _to_livekit_frame(audio_bytes: bytes, sample_rate: int, num_channels: int) -> rtc.AudioFrame:
    """Convert raw PCM bytes to LiveKit AudioFrame."""
    samples_per_channel = len(audio_bytes) // (2 * num_channels)
    return rtc.AudioFrame(
        data=audio_bytes,
        sample_rate=sample_rate,
        num_channels=num_channels,
        samples_per_channel=samples_per_channel,
    )


class SipAudioInput(AudioInput):
    """Async iterator yielding AudioFrames from a SIP call.

    Uses recv_audio_bytes_blocking() — blocks in Rust thread, GIL released.
    No Python polling loop, no asyncio.sleep jitter.
    """

    def __init__(self, endpoint, call_id: int):
        self._ep = endpoint
        self._cid = call_id
        self._closed = False

    async def __anext__(self) -> rtc.AudioFrame:
        loop = asyncio.get_event_loop()
        while not self._closed:
            result = await loop.run_in_executor(
                None, lambda: self._ep.recv_audio_bytes_blocking(self._cid, 20)
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


class SipAudioOutput(AudioOutput):
    """Sends AudioFrames to a SIP call. Uses raw bytes API."""

    def __init__(self, endpoint, call_id: int):
        super().__init__()
        self._ep = endpoint
        self._cid = call_id

    @property
    def sample_rate(self) -> Optional[int]:
        return 16000

    @property
    def can_pause(self) -> bool:
        return True

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        # Send raw PCM bytes directly — no Python int16 list conversion
        self._ep.send_audio_bytes(self._cid, bytes(frame.data), frame.sample_rate, frame.num_channels)

    def flush(self) -> None:
        self._ep.flush(self._cid)

    def clear_buffer(self) -> None:
        self._ep.clear_buffer(self._cid)

    def pause(self) -> None:
        self._ep.pause(self._cid)

    def resume(self) -> None:
        self._ep.resume(self._cid)

    def on_attached(self) -> None:
        pass

    def on_detached(self) -> None:
        pass
