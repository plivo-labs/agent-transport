"""LiveKit Agents AudioInput/AudioOutput adapters for SIP transport.

Usage:
    from agent_transport import SipEndpoint
    from agent_transport_adapters.livekit import SipAudioInput, SipAudioOutput

    ep = SipEndpoint(sip_server="phone.plivo.com")
    ep.register(username, password)
    call_id = ep.call(dest_uri)

    # Connect to LiveKit agent session
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

from agent_transport import SipEndpoint, AudioFrame


def _to_livekit_frame(frame: AudioFrame) -> rtc.AudioFrame:
    """Convert agent-transport AudioFrame to LiveKit AudioFrame."""
    pcm_bytes = bytes(
        b for s in frame.data for b in s.to_bytes(2, byteorder="little", signed=True)
    )
    return rtc.AudioFrame(
        data=pcm_bytes,
        sample_rate=frame.sample_rate,
        num_channels=frame.num_channels,
        samples_per_channel=frame.samples_per_channel,
    )


def _from_livekit_frame(frame: rtc.AudioFrame) -> AudioFrame:
    """Convert LiveKit AudioFrame to agent-transport AudioFrame."""
    data = list(
        int.from_bytes(frame.data[i : i + 2], byteorder="little", signed=True)
        for i in range(0, len(frame.data), 2)
    )
    return AudioFrame(data, frame.sample_rate, frame.num_channels)


class SipAudioInput(AudioInput):
    """Async iterator yielding AudioFrames from a SIP call.

    Uses recv_audio_blocking() which releases the GIL while waiting.
    No Python polling loop — avoids jitter at high concurrency.
    """

    def __init__(self, endpoint: SipEndpoint, call_id: int):
        self._ep = endpoint
        self._cid = call_id
        self._closed = False

    async def __anext__(self) -> rtc.AudioFrame:
        while not self._closed:
            # Block in Rust (GIL released), not Python asyncio.sleep
            frame = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._ep.recv_audio_blocking(self._cid, 20)
            )
            if frame is not None:
                return _to_livekit_frame(frame)
        raise StopAsyncIteration

    def __aiter__(self):
        return self

    def on_attached(self) -> None:
        pass

    def on_detached(self) -> None:
        self._closed = True


class SipAudioOutput(AudioOutput):
    """Sends AudioFrames to a SIP call.

    Implements LiveKit's AudioOutput interface with flush, clear_buffer,
    pause, and resume mapped to the SIP transport.
    """

    def __init__(self, endpoint: SipEndpoint, call_id: int):
        super().__init__()
        self._ep = endpoint
        self._cid = call_id
        self._pushed_duration = 0.0

    @property
    def sample_rate(self) -> Optional[int]:
        return 16000

    @property
    def can_pause(self) -> bool:
        return True

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        agent_frame = _from_livekit_frame(frame)
        self._ep.send_audio(self._cid, agent_frame)
        self._pushed_duration += frame.samples_per_channel / frame.sample_rate

    def flush(self) -> None:
        self._ep.flush(self._cid)

    def clear_buffer(self) -> None:
        self._ep.clear_buffer(self._cid)
        self._pushed_duration = 0.0

    def pause(self) -> None:
        self._ep.pause(self._cid)

    def resume(self) -> None:
        self._ep.resume(self._cid)

    def on_attached(self) -> None:
        pass

    def on_detached(self) -> None:
        pass
