"""SipAudioSource — drop-in equivalent of rtc.AudioSource for SIP/RTP transport.

Replicates rtc.AudioSource's exact behavior:
- capture_frame() tracks _q_size and sends to Rust via executor
- wait_for_playout() returns a Future resolved by call_later timer
- clear_queue() discards queued audio and releases waiters
- queued_duration property tracks remaining playout time

The timing math is copied line-for-line from rtc.AudioSource.
"""

import asyncio
import functools
import logging
import time
from typing import Optional

from livekit import rtc

logger = logging.getLogger(__name__)


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
        queue_size_ms: int = 200,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._ep = endpoint
        self._id = call_or_session_id
        self._sample_rate = sample_rate
        self._num_channels = num_channels
        self._queue_size_ms = queue_size_ms
        self._loop = loop or asyncio.get_event_loop()
        self._disposed = False

        # -- Exact copies of rtc.AudioSource fields --
        self._last_capture = 0.0
        self._q_size = 0.0
        self._join_handle: asyncio.TimerHandle | None = None
        self._join_fut: asyncio.Future[None] | None = None

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def num_channels(self) -> int:
        return self._num_channels

    @property
    def queued_duration(self) -> float:
        """Current duration (in seconds) of audio data queued for playback.

        Exact same formula as rtc.AudioSource.queued_duration.
        """
        return max(self._q_size - time.monotonic() + self._last_capture, 0.0)

    def clear_queue(self) -> None:
        """Clear the queue and release any pending wait_for_playout waiter.

        Matches rtc.AudioSource.clear_queue exactly.
        """
        self._ep.clear_buffer(self._id)
        self._release_waiter()

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        """Capture a frame and send it to the Rust transport layer.

        Tracks queue duration using the exact same timing math as rtc.AudioSource:
        _q_size += frame_duration - elapsed_since_last_capture

        Then sends the frame to Rust via executor (non-blocking).
        The call_later timer is set/updated to resolve wait_for_playout
        at exactly the right time.
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

        self._join_handle = self._loop.call_later(self._q_size, self._release_waiter)


        # -- Send frame to Rust (our equivalent of the FFI capture call) --
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

    async def wait_for_playout(self) -> None:
        """Wait for all queued audio to finish playing out.

        Exact copy of rtc.AudioSource.wait_for_playout:
        returns immediately if no frames captured, otherwise waits
        for the call_later timer to resolve the join future.
        """
        if self._join_fut is None:
            return

        await asyncio.shield(self._join_fut)

    def _release_waiter(self) -> None:
        """Release the playout waiter. Exact copy of rtc.AudioSource._release_waiter."""
        if self._join_fut is None:
            return

        if not self._join_fut.done():
            self._join_fut.set_result(None)

        self._last_capture = 0.0
        self._q_size = 0.0
        self._join_fut = None

    async def aclose(self) -> None:
        """Close the audio source."""
        self._disposed = True
        self._release_waiter()
