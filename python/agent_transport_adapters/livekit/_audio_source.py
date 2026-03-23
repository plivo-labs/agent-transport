"""SipAudioSource — drop-in equivalent of rtc.AudioSource for SIP/RTP transport.

Matches WebRTC's async backpressure pattern exactly:
- capture_frame() pushes audio to Rust buffer (sync, fast)
- If buffer <= threshold: Rust fires completion callback immediately
- If buffer > threshold: Rust stores callback, fires when RTP loop drains
- Completion callback uses loop.call_soon_threadsafe to resolve a Python Future
- capture_frame() awaits the Future — async suspension, no thread blocked

This matches WebRTC's C++ deferred on_complete → Rust oneshot → Python callback pattern.
"""

import asyncio
import time

from livekit import rtc


class SipAudioSource:
    """Audio source that sends frames to a SIP/RTP or AudioStream endpoint.

    Matches rtc.AudioSource's backpressure and timing semantics exactly.
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

        # -- Timing fields (same as rtc.AudioSource for wait_for_playout) --
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
        """Current duration (in seconds) of audio data queued for playback."""
        return max(self._q_size - time.monotonic() + self._last_capture, 0.0)

    def clear_queue(self) -> None:
        """Clear the queue and release all pending waiters."""
        self._ep.clear_buffer(self._id)
        self._release_waiter()

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        """Capture a frame and send it to the Rust transport layer.

        Matches WebRTC's async backpressure pattern:
        1. Push audio to Rust buffer (sync, fast via send_audio_notify)
        2. Rust fires completion callback immediately if below threshold
        3. If above threshold, Rust defers callback until RTP loop drains
        4. Callback resolves a Python Future via loop.call_soon_threadsafe
        5. We await the Future — async suspension, no thread blocked

        This is the exact same pattern as WebRTC's:
        Python → FFI request → C++ buffer → deferred on_complete → Rust oneshot →
        FFI callback event → Python Future resolves
        """
        if frame.samples_per_channel == 0 or self._disposed:
            return

        # -- Timing math (same as rtc.AudioSource for wait_for_playout) --
        now = time.monotonic()
        elapsed = 0.0 if self._last_capture == 0.0 else now - self._last_capture
        self._q_size += frame.samples_per_channel / self.sample_rate - elapsed
        self._last_capture = now

        if self._join_handle:
            self._join_handle.cancel()

        if self._join_fut is None:
            self._join_fut = self._loop.create_future()

        self._join_handle = self._loop.call_later(self._q_size, self._release_waiter)

        # -- Push to Rust with async completion notification --
        # Create a Future that Rust will resolve via the completion callback
        capture_fut = self._loop.create_future()

        def _on_complete():
            """Called from Rust RTP thread when buffer drains below threshold.
            Uses call_soon_threadsafe to safely resolve the Python Future."""
            try:
                self._loop.call_soon_threadsafe(capture_fut.set_result, None)
            except RuntimeError:
                pass  # event loop closed

        # Push audio to Rust — Rust fires _on_complete immediately if below
        # threshold, or defers it until RTP loop drains
        self._ep.send_audio_notify(
            self._id,
            bytes(frame.data),
            frame.sample_rate,
            frame.num_channels,
            _on_complete,
        )

        # Await completion — instant if below threshold, suspends if above
        await capture_fut

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

    async def aclose(self) -> None:
        """Close the audio source."""
        self._disposed = True
