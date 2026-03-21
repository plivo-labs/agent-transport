"""LiveKit Agents AudioInput/AudioOutput adapters for Plivo audio streaming.

Same semantics as SIP adapter — flush/clear_buffer/pause match LiveKit exactly.
"""

import asyncio
import time
from typing import Optional

try:
    from livekit import rtc
    from livekit.agents.voice.io import AudioInput, AudioOutput
    try:
        from livekit.agents.voice.io import PlaybackStartedEvent, PlaybackFinishedEvent
    except ImportError:
        from dataclasses import dataclass
        @dataclass
        class PlaybackStartedEvent:
            created_at: float
        @dataclass
        class PlaybackFinishedEvent:
            playback_position: float
            interrupted: bool
            synchronized_transcript: Optional[str] = None
except ImportError:
    raise ImportError("livekit-agents is required: pip install livekit-agents")

from .sip_io import _to_livekit_frame


class AudioStreamInput(AudioInput):
    def __init__(self, endpoint, session_id: int, *, label: str = "audio-stream-input", source=None, **kwargs):
        self._ep = endpoint
        self._sid = session_id
        self._label = label
        self._source = source
        self._closed = False

    @property
    def label(self) -> str: return self._label
    @property
    def source(self): return self._source

    async def __anext__(self) -> rtc.AudioFrame:
        loop = asyncio.get_event_loop()
        while not self._closed:
            result = await loop.run_in_executor(
                None, lambda: self._ep.recv_audio_bytes_blocking(self._sid, 20)
            )
            if result is not None:
                ab, sr, nc = result
                return _to_livekit_frame(bytes(ab), sr, nc)
        raise StopAsyncIteration

    def __aiter__(self): return self
    def on_attached(self) -> None: pass
    def on_detached(self) -> None: self._closed = True
    def __repr__(self) -> str: return f"AudioStreamInput(label={self._label!r})"


class AudioStreamOutput(AudioOutput):
    """Matches LiveKit _ParticipantAudioOutput semantics for audio streaming."""

    def __init__(self, endpoint, session_id: int, *, label: str = "audio-stream-output",
                 capabilities=None, sample_rate: Optional[int] = None,
                 next_in_chain=None, **kwargs):
        try:
            kw = {"label": label, "sample_rate": sample_rate, "next_in_chain": next_in_chain}
            if capabilities is not None: kw["capabilities"] = capabilities
            kw.update(kwargs)
            super().__init__(**kw)
        except TypeError:
            super().__init__()

        self._ep = endpoint
        self._sid = session_id
        self._label_str = label
        self._sample_rate = sample_rate
        self._next_in_chain = next_in_chain
        self._capturing = False
        self._segment_count = 0
        self._finished_count = 0
        self._pushed_duration = 0.0
        self._interrupted_event = asyncio.Event()
        self._playback_finished_event = asyncio.Event()
        self._last_ev: Optional[PlaybackFinishedEvent] = None
        self._flush_task: Optional[asyncio.Task] = None
    @property
    def label(self) -> str: return self._label_str
    @property
    def sample_rate(self) -> Optional[int]: return self._sample_rate or self._ep.sample_rate
    @property
    def can_pause(self) -> bool: return True
    @property
    def next_in_chain(self): return self._next_in_chain

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        if not self._capturing:
            self._capturing = True
            self._segment_count += 1
            self._interrupted_event.clear()
            self.on_playback_started(created_at=time.time())
        if frame.sample_rate > 0:
            self._pushed_duration += frame.samples_per_channel / frame.sample_rate
        self._ep.send_audio_bytes(self._sid, bytes(frame.data), frame.sample_rate, frame.num_channels)

    def flush(self) -> None:
        try: self._ep.flush(self._sid)
        except Exception: pass
        if not self._pushed_duration: return
        if self._flush_task and not self._flush_task.done(): self._flush_task.cancel()
        self._flush_task = asyncio.ensure_future(self._async_wait())

    def clear_buffer(self) -> None:
        self._ep.clear_buffer(self._sid)
        if self._pushed_duration: self._interrupted_event.set()

    async def _async_wait(self) -> None:
        wi = asyncio.create_task(self._interrupted_event.wait())
        wp = asyncio.create_task(self._wait_playout_rust())
        done, _ = await asyncio.wait([wi, wp], return_when=asyncio.FIRST_COMPLETED)
        interrupted = wi in done
        pushed = self._pushed_duration
        if interrupted:
            queued = self._ep.queued_frames(self._sid) * 0.02
            pushed = max(pushed - queued, 0.0)
            wp.cancel()
        else:
            wi.cancel()
        self._pushed_duration = 0.0
        self._capturing = False
        self._interrupted_event.clear()
        self.on_playback_finished(playback_position=pushed, interrupted=interrupted)

    async def _wait_playout_rust(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self._ep.wait_for_playout(self._sid, 30000))

    def on_playback_started(self, *, created_at: float) -> None:
        try: self.emit("playback_started", PlaybackStartedEvent(created_at=created_at))
        except Exception: pass

    def on_playback_finished(self, *, playback_position: float, interrupted: bool, synchronized_transcript: Optional[str] = None) -> None:
        self._finished_count += 1
        self._last_ev = PlaybackFinishedEvent(playback_position=playback_position, interrupted=interrupted, synchronized_transcript=synchronized_transcript)
        self._playback_finished_event.set()
        try: self.emit("playback_finished", self._last_ev)
        except Exception: pass

    async def wait_for_playout(self) -> Optional[PlaybackFinishedEvent]:
        if self._segment_count <= self._finished_count: return self._last_ev
        self._playback_finished_event.clear()
        await self._playback_finished_event.wait()
        return self._last_ev

    def _reset_playback_count(self) -> None:
        self._segment_count = 0
        self._finished_count = 0
        self._pushed_duration = 0.0

    def pause(self) -> None:
        self._ep.pause(self._sid)
        if self._next_in_chain: self._next_in_chain.pause()

    def resume(self) -> None:
        self._ep.resume(self._sid)
        if self._next_in_chain: self._next_in_chain.resume()

    def on_attached(self) -> None: pass
    def on_detached(self) -> None: pass
    def __repr__(self) -> str: return f"AudioStreamOutput(label={self._label_str!r})"
