"""LiveKit Agents AudioInput/AudioOutput adapters for Plivo audio streaming.

Uses LiveKit base class implementations for:
- Segment counting (__playback_segments_count via super().capture_frame)
- wait_for_playout (base class asyncio.Event + __playback_finished_count)
- on_playback_started / on_playback_finished (base class EventEmitter)
- pause/resume chain propagation (base class next_in_chain)
"""

import asyncio
import logging
import time
from typing import Optional

try:
    from livekit import rtc
    from livekit.agents.voice.io import AudioInput, AudioOutput
    try:
        from livekit.agents.voice.io import PlaybackStartedEvent, PlaybackFinishedEvent, AudioOutputCapabilities
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
        @dataclass
        class AudioOutputCapabilities:
            pause: bool
except ImportError:
    raise ImportError("livekit-agents is required: pip install livekit-agents")

from .sip_io import _to_livekit_frame

logger = logging.getLogger(__name__)


class AudioStreamInput(AudioInput):
    def __init__(self, endpoint, session_id: int, *, label: str = "audio-stream-input", source=None, **kwargs):
        # Call base class __init__ to set __label and __source
        try:
            super().__init__(label=label, source=source)
        except TypeError:
            pass  # Fallback for versions without these params
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
        # Delegate to source if set (matches base class)
        if self._source:
            return await self._source.__anext__()

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

    def on_attached(self) -> None:
        # Propagate to source (matches base class)
        if self._source:
            self._source.on_attached()

    def on_detached(self) -> None:
        self._closed = True
        # Propagate to source (matches base class)
        if self._source:
            self._source.on_detached()

    def __repr__(self) -> str:
        return f"AudioStreamInput(label={self._label!r}, source={self._source!r})"


class AudioStreamOutput(AudioOutput):
    """Sends AudioFrames to Plivo audio stream.

    Relies on base class for: segment counting, wait_for_playout,
    playback events, pause/resume chain propagation.
    """

    def __init__(self, endpoint, session_id: int, *, label: str = "audio-stream-output",
                 capabilities=None, sample_rate: Optional[int] = None,
                 next_in_chain=None, **kwargs):
        if capabilities is None:
            capabilities = AudioOutputCapabilities(pause=True)

        super().__init__(
            label=label,
            capabilities=capabilities,
            sample_rate=sample_rate,
            next_in_chain=next_in_chain,
            **kwargs,
        )

        self._ep = endpoint
        self._sid = session_id
        self._label_str = label
        self._pushed_duration = 0.0
        self._interrupted_event = asyncio.Event()
        self._flush_task: Optional[asyncio.Task] = None
        self._first_frame_sent = False

    @property
    def sample_rate(self) -> Optional[int]:
        return self._ep.sample_rate

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        # Base class tracks __capturing and __playback_segments_count
        await super().capture_frame(frame)

        # Wait for in-progress flush (matches LiveKit: logs error + awaits)
        if self._flush_task and not self._flush_task.done():
            logger.error("capture_frame called while flush is in progress")
            await self._flush_task

        # Send to Rust transport
        self._ep.send_audio_bytes(self._sid, bytes(frame.data), frame.sample_rate, frame.num_channels)

        # Track duration for playback position calculation
        if frame.sample_rate > 0:
            self._pushed_duration += frame.samples_per_channel / frame.sample_rate

        # Emit playback_started AFTER frame sent (matches LiveKit _forward_audio)
        if not self._first_frame_sent:
            self._first_frame_sent = True
            self.on_playback_started(created_at=time.time())

    def flush(self) -> None:
        # Base class sets __capturing = False
        super().flush()

        # Send checkpoint to Plivo
        try: self._ep.flush(self._sid)
        except Exception: pass

        if not self._pushed_duration:
            return

        if self._flush_task and not self._flush_task.done():
            logger.error("flush called while playback is in progress")
            self._flush_task.cancel()
        self._flush_task = asyncio.ensure_future(self._async_wait_for_playout())

    def clear_buffer(self) -> None:
        self._ep.clear_buffer(self._sid)
        if self._pushed_duration:
            self._interrupted_event.set()

    async def _async_wait_for_playout(self) -> None:
        """Race playout completion vs interruption (matches LiveKit _wait_for_playout)."""
        wait_interrupt = asyncio.create_task(self._interrupted_event.wait())
        wait_playout = asyncio.create_task(self._wait_playout_rust())

        done, _ = await asyncio.wait(
            [wait_interrupt, wait_playout],
            return_when=asyncio.FIRST_COMPLETED,
        )

        interrupted = wait_interrupt in done
        pushed = self._pushed_duration

        if interrupted:
            queued = self._ep.queued_frames(self._sid) * 0.02
            pushed = max(pushed - queued, 0.0)
            wait_playout.cancel()
        else:
            wait_interrupt.cancel()

        # Reset state for next segment (matches LiveKit cleanup)
        self._pushed_duration = 0.0
        self._interrupted_event.clear()
        self._first_frame_sent = False

        # Base class handles counting, event, __playback_finished_event
        self.on_playback_finished(
            playback_position=pushed,
            interrupted=interrupted,
        )

    async def _wait_playout_rust(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: self._ep.wait_for_playout(self._sid, 30000)
        )

    def pause(self) -> None:
        super().pause()  # Base class propagates to next_in_chain
        self._ep.pause(self._sid)

    def resume(self) -> None:
        super().resume()  # Base class propagates to next_in_chain
        self._ep.resume(self._sid)
        self._first_frame_sent = False  # Reset for next segment

    def on_attached(self) -> None: pass
    def on_detached(self) -> None: pass

    def __repr__(self) -> str:
        return f"AudioStreamOutput(label={self._label_str!r})"
