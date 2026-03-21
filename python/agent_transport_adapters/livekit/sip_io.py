"""LiveKit Agents AudioInput/AudioOutput adapters for SIP transport.

Matches LiveKit's _ParticipantAudioOutput behavior exactly:
- flush() is non-blocking, starts async playout wait, emits playback_finished when done
- clear_buffer() sets interrupted flag, playout wait detects and emits
- pause() stops forwarding but preserves capture_frame() calls (no queue drain)
- resume() restarts forwarding
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

try:
    from livekit import rtc
    from livekit.agents.voice.io import AudioInput, AudioOutput
    try:
        from livekit.agents.voice.io import PlaybackStartedEvent, PlaybackFinishedEvent
    except ImportError:
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


def _to_livekit_frame(audio_bytes: bytes, sample_rate: int, num_channels: int) -> rtc.AudioFrame:
    samples_per_channel = len(audio_bytes) // (2 * num_channels)
    return rtc.AudioFrame(
        data=audio_bytes, sample_rate=sample_rate,
        num_channels=num_channels, samples_per_channel=samples_per_channel,
    )


class SipAudioInput(AudioInput):
    """Async iterator yielding AudioFrames from a SIP call."""

    def __init__(self, endpoint, call_id: int, *, label: str = "sip-audio-input", source=None, **kwargs):
        self._ep = endpoint
        self._cid = call_id
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
                None, lambda: self._ep.recv_audio_bytes_blocking(self._cid, 20)
            )
            if result is not None:
                ab, sr, nc = result
                return _to_livekit_frame(bytes(ab), sr, nc)
        raise StopAsyncIteration

    def __aiter__(self): return self
    def on_attached(self) -> None: pass
    def on_detached(self) -> None: self._closed = True
    def __repr__(self) -> str: return f"SipAudioInput(label={self._label!r})"


class SipAudioOutput(AudioOutput):
    """Sends AudioFrames to a SIP call — matches LiveKit _ParticipantAudioOutput."""

    def __init__(self, endpoint, call_id: int, *, label: str = "sip-audio-output",
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
        self._cid = call_id
        self._label_str = label
        self._capabilities = capabilities
        self._sample_rate = sample_rate
        self._next_in_chain = next_in_chain

        # Playback state (matches LiveKit internals)
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
    def next_in_chain(self): return self._next_in_chain

    @property
    def can_pause(self) -> bool:
        if self._capabilities and hasattr(self._capabilities, 'pause') and not self._capabilities.pause:
            return False
        if self._next_in_chain and not self._next_in_chain.can_pause:
            return False
        return True

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        # Accept frames even when paused (LiveKit behavior — frames queue up)
        if not self._capturing:
            self._capturing = True
            self._segment_count += 1
            self._interrupted_event.clear()
            self.on_playback_started(created_at=time.time())

        # Track duration for playback position calculation
        if frame.sample_rate > 0:
            self._pushed_duration += frame.samples_per_channel / frame.sample_rate

        self._ep.send_audio_bytes(self._cid, bytes(frame.data), frame.sample_rate, frame.num_channels)

    def flush(self) -> None:
        """Non-blocking flush — starts async playout wait (matches LiveKit)."""
        self._ep.flush(self._cid)
        if not self._pushed_duration:
            return
        # Cancel any existing flush task
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        self._flush_task = asyncio.ensure_future(self._async_wait_for_playout())

    def clear_buffer(self) -> None:
        """Clear buffer and signal interruption (matches LiveKit)."""
        self._ep.clear_buffer(self._cid)
        if self._pushed_duration:
            self._interrupted_event.set()

    async def _async_wait_for_playout(self) -> None:
        """Wait for playout or interruption, then emit playback_finished."""
        wait_interrupt = asyncio.create_task(self._interrupted_event.wait())
        wait_playout = asyncio.create_task(self._wait_rust_playout())

        done, pending = await asyncio.wait(
            [wait_interrupt, wait_playout],
            return_when=asyncio.FIRST_COMPLETED,
        )

        interrupted = wait_interrupt in done
        pushed = self._pushed_duration

        if interrupted:
            # Calculate how much actually played (approximate)
            queued = self._ep.queued_frames(self._cid) * 0.02
            pushed = max(pushed - queued, 0.0)
            wait_playout.cancel()
        else:
            wait_interrupt.cancel()

        self._pushed_duration = 0.0
        self._capturing = False
        self._interrupted_event.clear()

        self.on_playback_finished(
            playback_position=pushed,
            interrupted=interrupted,
        )

    async def _wait_rust_playout(self) -> None:
        """Wait for Rust transport queue to drain."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: self._ep.wait_for_playout(self._cid, 30000)
        )

    def on_playback_started(self, *, created_at: float) -> None:
        ev = PlaybackStartedEvent(created_at=created_at)
        try: self.emit("playback_started", ev)
        except Exception: pass

    def on_playback_finished(self, *, playback_position: float, interrupted: bool,
                              synchronized_transcript: Optional[str] = None) -> None:
        self._finished_count += 1
        self._last_ev = PlaybackFinishedEvent(
            playback_position=playback_position, interrupted=interrupted,
            synchronized_transcript=synchronized_transcript,
        )
        self._playback_finished_event.set()
        try: self.emit("playback_finished", self._last_ev)
        except Exception: pass

    async def wait_for_playout(self) -> Optional[PlaybackFinishedEvent]:
        if self._segment_count <= self._finished_count:
            return self._last_ev
        self._playback_finished_event.clear()
        await self._playback_finished_event.wait()
        return self._last_ev

    def _reset_playback_count(self) -> None:
        self._segment_count = 0
        self._finished_count = 0
        self._pushed_duration = 0.0

    def pause(self) -> None:
        """Pause — send loop outputs nothing, frames accumulate in Rust channel."""
        self._ep.pause(self._cid)
        if self._next_in_chain: self._next_in_chain.pause()

    def resume(self) -> None:
        self._ep.resume(self._cid)
        if self._next_in_chain: self._next_in_chain.resume()

    def on_attached(self) -> None: pass
    def on_detached(self) -> None: pass
    def __repr__(self) -> str: return f"SipAudioOutput(label={self._label_str!r})"
