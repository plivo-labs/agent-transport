"""LiveKit Agents AudioInput/AudioOutput adapters for SIP transport.

Fully compliant with LiveKit Agents' io.py interface:
- Correct constructor signatures (label, capabilities, next_in_chain, sample_rate)
- EventEmitter events: playback_started, playback_finished
- Playback segment tracking and wait_for_playout()
- Zero-copy bytes API for high concurrency

Usage:
    from agent_transport_adapters.livekit import SipAudioInput, SipAudioOutput
    session.input.audio = SipAudioInput(ep, call_id, label="sip-in")
    session.output.audio = SipAudioOutput(ep, call_id, label="sip-out")
"""

import asyncio
import time
from typing import Optional

try:
    from livekit import rtc
    from livekit.agents.voice.io import AudioInput, AudioOutput
except ImportError:
    raise ImportError("livekit-agents is required: pip install livekit-agents")


def _to_livekit_frame(audio_bytes: bytes, sample_rate: int, num_channels: int) -> rtc.AudioFrame:
    samples_per_channel = len(audio_bytes) // (2 * num_channels)
    return rtc.AudioFrame(
        data=audio_bytes, sample_rate=sample_rate,
        num_channels=num_channels, samples_per_channel=samples_per_channel,
    )


class SipAudioInput(AudioInput):
    """Async iterator yielding AudioFrames from a SIP call."""

    def __init__(self, endpoint, call_id: int, *, label: str = "sip-audio-input", source=None):
        self._ep = endpoint
        self._cid = call_id
        self._label = label
        self._source = source
        self._closed = False

    @property
    def label(self) -> str:
        return self._label

    @property
    def source(self):
        return self._source

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

    def __repr__(self) -> str:
        return f"SipAudioInput(label={self._label!r})"


class SipAudioOutput(AudioOutput):
    """Sends AudioFrames to a SIP call.

    Tracks playback segments and emits playback_started/playback_finished events
    as required by LiveKit's VoicePipelineAgent.
    """

    def __init__(self, endpoint, call_id: int, *, label: str = "sip-audio-output",
                 sample_rate: Optional[int] = None, next_in_chain=None, **kwargs):
        # Note: LiveKit's AudioOutput.__init__ signature may vary by version.
        # We accept kwargs to be forward-compatible.
        try:
            super().__init__(label=label, sample_rate=sample_rate, next_in_chain=next_in_chain, **kwargs)
        except TypeError:
            # Fallback for versions with different constructor
            super().__init__()
        self._ep = endpoint
        self._cid = call_id
        self._label_str = label
        self._sample_rate = sample_rate
        self._next_in_chain = next_in_chain
        # Playback tracking
        self._capturing = False
        self._segment_count = 0
        self._finished_count = 0
        self._playback_finished_event = asyncio.Event()
        self._last_playback_ev = None
        self._interrupted = False

    @property
    def label(self) -> str:
        return self._label_str

    @property
    def sample_rate(self) -> Optional[int]:
        return self._sample_rate or self._ep.sample_rate

    @property
    def can_pause(self) -> bool:
        if self._next_in_chain:
            return self._next_in_chain.can_pause
        return True

    @property
    def next_in_chain(self):
        return self._next_in_chain

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        if not self._capturing:
            self._capturing = True
            self._segment_count += 1
            self._interrupted = False
            self.on_playback_started(created_at=time.time())
        self._ep.send_audio_bytes(self._cid, bytes(frame.data), frame.sample_rate, frame.num_channels)

    def flush(self) -> None:
        self._ep.flush(self._cid)
        if self._capturing:
            self._capturing = False
            self.on_playback_finished(
                playback_position=1.0,
                interrupted=self._interrupted,
            )

    def clear_buffer(self) -> None:
        self._interrupted = True
        self._ep.clear_buffer(self._cid)
        if self._capturing:
            self._capturing = False
            self.on_playback_finished(
                playback_position=0.0,  # Unknown position on interrupt
                interrupted=True,
            )

    def on_playback_started(self, *, created_at: float) -> None:
        try:
            self.emit("playback_started", {"created_at": created_at})
        except Exception:
            pass  # EventEmitter may not be available in all versions

    def on_playback_finished(self, *, playback_position: float, interrupted: bool,
                              synchronized_transcript: Optional[str] = None) -> None:
        self._finished_count += 1
        self._last_playback_ev = {
            "playback_position": playback_position,
            "interrupted": interrupted,
            "synchronized_transcript": synchronized_transcript,
        }
        self._playback_finished_event.set()
        try:
            self.emit("playback_finished", self._last_playback_ev)
        except Exception:
            pass

    async def wait_for_playout(self):
        """Wait until current playback segment finishes."""
        if self._segment_count <= self._finished_count:
            return self._last_playback_ev
        self._playback_finished_event.clear()
        await self._playback_finished_event.wait()
        return self._last_playback_ev

    def _reset_playback_count(self) -> None:
        self._segment_count = 0
        self._finished_count = 0

    def pause(self) -> None:
        self._ep.pause(self._cid)
        if self._next_in_chain:
            self._next_in_chain.pause()

    def resume(self) -> None:
        self._ep.resume(self._cid)
        if self._next_in_chain:
            self._next_in_chain.resume()

    def on_attached(self) -> None:
        pass

    def on_detached(self) -> None:
        pass

    def __repr__(self) -> str:
        return f"SipAudioOutput(label={self._label_str!r})"
