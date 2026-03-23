"""SipAudioSource — drop-in equivalent of rtc.AudioSource for SIP/RTP transport.

Replicates rtc.AudioSource's exact behavior including backpressure:
- capture_frame() tracks _q_size and provides natural pacing
- When buffer > notify_threshold (queue_size_ms), capture_frame WAITS
  until the playout timer drains it — matching WebRTC C++ behavior
- wait_for_playout() returns a Future resolved by call_later timer
- clear_queue() discards queued audio and releases waiters
- queued_duration property tracks remaining playout time

The WebRTC C++ AudioSource works as follows:
- Buffer capacity = 2 × queue_size_samples
- notify_threshold = queue_size_samples
- If buffer ≤ threshold: on_complete fires immediately (fast return)
- If buffer > threshold: on_complete deferred until playout drains (BLOCKS)
- If buffer ≥ capacity: frame rejected

We replicate this timing-based backpressure in Python.
"""

import asyncio
import functools
import time
from typing import Optional

from livekit import rtc


class SipAudioSource:
    """Audio source that sends frames to a SIP/RTP or AudioStream endpoint.

    Matches rtc.AudioSource's queue tracking and backpressure semantics exactly.
    """

    def __init__(
        self,
        endpoint,
        call_or_session_id: int,
        sample_rate: int,
        num_channels: int = 1,
        queue_size_ms: int = 1000,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._ep = endpoint
        self._id = call_or_session_id
        self._sample_rate = sample_rate
        self._num_channels = num_channels
        self._queue_size_ms = queue_size_ms
        self._loop = loop or asyncio.get_event_loop()
        self._disposed = False

        # Threshold in seconds (matches WebRTC notify_threshold = queue_size_samples)
        self._notify_threshold = queue_size_ms / 1000.0

        # -- Exact copies of rtc.AudioSource fields --
        self._last_capture = 0.0
        self._q_size = 0.0
        self._join_handle: asyncio.TimerHandle | None = None
        self._join_fut: asyncio.Future[None] | None = None

        # Backpressure: when _q_size > threshold, capture_frame waits
        # for this future to resolve (set by call_later when _q_size drains)
        self._capture_wait_handle: asyncio.TimerHandle | None = None
        self._capture_wait_fut: asyncio.Future[None] | None = None

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def num_channels(self) -> int:
        return self._num_channels

    @property
    def queued_duration(self) -> float:
        """Current duration (in seconds) of audio data queued for playback."""
        return max(self._q_size - time.monotonic() + self._last_capture, 0.0)

    def clear_queue(self) -> None:
        """Clear the queue and release all pending waiters."""
        self._ep.clear_buffer(self._id)
        self._release_waiter()
        self._release_capture_waiter()

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        """Capture a frame and send it to the Rust transport layer.

        Provides natural pacing matching WebRTC C++ AudioSource:
        - If _q_size ≤ notify_threshold: send immediately, return fast
        - If _q_size > notify_threshold: send, then WAIT until _q_size
          drains below threshold (via call_later timer)

        This is what makes the forwarding task naturally pace at real-time,
        which backs up the Chan, which backs up _audio_forwarding_task,
        which backs up TTS consumption, which slows down the pipeline.
        """
        if frame.samples_per_channel == 0 or self._disposed:
            return

        # -- Timing math: line-for-line copy of rtc.AudioSource.capture_frame --
        now = time.monotonic()
        elapsed = 0.0 if self._last_capture == 0.0 else now - self._last_capture
        self._q_size += frame.samples_per_channel / self.sample_rate - elapsed
        self._last_capture = now

        if self._join_handle:
            self._join_handle.cancel()

        if self._join_fut is None:
            self._join_fut = self._loop.create_future()

        self._join_handle = self._loop.call_later(max(self._q_size, 0), self._release_waiter)

        # -- Send frame to Rust via executor (avoids blocking asyncio loop) --
        await self._loop.run_in_executor(
            None,
            functools.partial(
                self._ep.send_audio_bytes,
                self._id,
                bytes(frame.data),
                frame.sample_rate,
                frame.num_channels,
            ),
        )

        # -- Backpressure: if buffer > threshold, wait for drain --
        # This matches WebRTC C++ behavior where on_complete is deferred
        # when buffer exceeds notify_threshold. The caller (forwarding task)
        # blocks until the playout timer drains below threshold.
        if self._q_size > self._notify_threshold:
            # Calculate how long until _q_size drains to threshold
            wait_time = self._q_size - self._notify_threshold
            if self._capture_wait_fut is not None and not self._capture_wait_fut.done():
                self._capture_wait_handle.cancel()
            self._capture_wait_fut = self._loop.create_future()
            self._capture_wait_handle = self._loop.call_later(
                wait_time, self._release_capture_waiter
            )
            await self._capture_wait_fut

    async def wait_for_playout(self) -> None:
        """Wait for all queued audio to finish playing out."""
        if self._join_fut is None:
            return

        await asyncio.shield(self._join_fut)

    def _release_waiter(self) -> None:
        """Release the playout waiter."""
        if self._join_fut is None:
            return

        if not self._join_fut.done():
            self._join_fut.set_result(None)

        self._last_capture = 0.0
        self._q_size = 0.0
        self._join_fut = None

    def _release_capture_waiter(self) -> None:
        """Release the capture backpressure waiter."""
        if self._capture_wait_fut is not None and not self._capture_wait_fut.done():
            self._capture_wait_fut.set_result(None)
        self._capture_wait_fut = None
        self._capture_wait_handle = None

    async def aclose(self) -> None:
        """Close the audio source."""
        self._disposed = True
        self._release_waiter()
        self._release_capture_waiter()
