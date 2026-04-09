"""SipAudioSource — drop-in equivalent of rtc.AudioSource for SIP/RTP transport.

Matches WebRTC's AudioSource exactly:
- capture_frame() pushes audio with backpressure (callback when buffer has space)
- wait_for_playout() waits for buffer to drain (Rust condvar, pause-aware)
- queued_duration returns real buffer state from Rust
- clear_queue() clears buffer immediately

No timer heuristics — all playout tracking comes from Rust.
"""

import asyncio
import logging

from livekit import rtc

logger = logging.getLogger(__name__)


class SipAudioSource:
    """Audio source that sends frames to a SIP/RTP or AudioStream endpoint.

    Matches rtc.AudioSource's backpressure and playout semantics exactly.
    """

    def __init__(
        self,
        endpoint,
        call_or_session_id: str,
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
        self._playout_fut: asyncio.Future[None] | None = None

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def num_channels(self) -> int:
        return self._num_channels

    @property
    def queued_duration(self) -> float:
        """Current duration (in seconds) of audio data queued for playback.

        Returns real Rust buffer state — matches WebRTC's audioSource.queuedDuration.
        """
        try:
            return self._ep.queued_duration_ms(self._id) / 1000.0
        except Exception:
            return 0.0

    def clear_queue(self) -> None:
        """Clear the queue immediately. Matches WebRTC's audioSource.clearQueue()."""
        self._ep.clear_buffer(self._id)

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        """Capture a frame and send it to the Rust transport layer.

        Matches WebRTC's async backpressure pattern:
        1. Push audio to Rust buffer (sync, fast via send_audio_notify)
        2. Rust fires completion callback immediately if below threshold
        3. If above threshold, Rust defers callback until RTP loop drains
        4. Callback resolves a Python Future via loop.call_soon_threadsafe
        5. We await the Future — async suspension, no thread blocked
        """
        if frame.samples_per_channel == 0 or self._disposed:
            return

        # Create a Future that Rust will resolve via the completion callback
        capture_fut = self._loop.create_future()

        def _on_complete():
            """Called from Rust RTP thread when buffer drains below threshold.
            Uses call_soon_threadsafe to safely resolve the Python Future."""
            def _resolve():
                if not capture_fut.done():
                    capture_fut.set_result(None)
            try:
                self._loop.call_soon_threadsafe(_resolve)
            except RuntimeError:
                try:
                    capture_fut.set_result(None)
                except Exception:
                    pass

        # Push audio to Rust — Rust fires _on_complete immediately if below
        # threshold, or defers it until RTP loop drains
        try:
            self._ep.send_audio_notify(
                self._id,
                bytes(frame.data),
                frame.sample_rate,
                frame.num_channels,
                _on_complete,
            )
        except Exception:
            raise

        # Await completion — instant if below threshold, suspends if above
        await capture_fut

    async def wait_for_playout(self) -> None:
        """Wait for all queued audio to finish playing out.

        Uses shared future pattern — matches WebRTC's rtc.AudioSource._join_fut.
        Multiple callers within one flush cycle share the same future.
        Rust callback fires when buffer drains to empty (pause-aware).
        """
        if self._playout_fut is None:
            self._playout_fut = self._loop.create_future()

            def _on_playout():
                """Called from Rust RTP thread when buffer empties."""
                def _resolve():
                    fut = self._playout_fut
                    if fut is not None and not fut.done():
                        fut.set_result(None)
                    self._playout_fut = None
                try:
                    self._loop.call_soon_threadsafe(_resolve)
                except RuntimeError:
                    try:
                        if self._playout_fut and not self._playout_fut.done():
                            self._playout_fut.set_result(None)
                        self._playout_fut = None
                    except Exception:
                        pass

            try:
                self._ep.wait_for_playout_notify(self._id, _on_playout)
            except Exception:
                self._playout_fut = None
                return

        await asyncio.shield(self._playout_fut)

    async def aclose(self) -> None:
        """Close the audio source."""
        self._disposed = True


class AudioStreamAudioSource(SipAudioSource):
    """Audio source that uses Plivo's checkpoint/playedStream for accurate playout confirmation.

    Instead of the Rust condvar used by SipAudioSource, this uses Plivo's
    server-side confirmation:
    - flush() sends a checkpoint to Plivo
    - wait_for_playout() waits for Plivo's playedStream event confirming audio was played
    - clear_queue() sends clearAudio to Plivo and clears local buffer

    Uses shared future pattern — multiple callers within one flush cycle share
    the same Plivo checkpoint confirmation (matches SipAudioSource._playout_fut).
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._plivo_playout_fut: asyncio.Future[None] | None = None

    async def wait_for_playout(self) -> None:
        """Wait for Plivo server confirmation that queued audio has been played.

        Uses shared future — first caller sends checkpoint, subsequent callers
        await the same future without sending duplicate checkpoints.
        """
        if self._plivo_playout_fut is None:
            self._plivo_playout_fut = self._loop.create_future()

            async def _wait():
                try:
                    self._ep.flush(self._id)
                except Exception:
                    logger.warning("AudioStreamAudioSource: flush failed for session %s", self._id, exc_info=True)
                    return

                loop = asyncio.get_running_loop()
                try:
                    confirmed = await loop.run_in_executor(
                        None, self._ep.wait_for_playout, self._id, 30000
                    )
                    if not confirmed:
                        logger.warning("AudioStreamAudioSource: wait_for_playout timed out on session %s", self._id)
                except Exception:
                    logger.warning("AudioStreamAudioSource: wait_for_playout error on session %s", self._id, exc_info=True)
                finally:
                    fut = self._plivo_playout_fut
                    if fut is not None and not fut.done():
                        fut.set_result(None)
                    self._plivo_playout_fut = None

            asyncio.create_task(_wait())

        await asyncio.shield(self._plivo_playout_fut)

    def clear_queue(self) -> None:
        """Clear buffer — sends clearAudio to Plivo + clears local AudioBuffer."""
        self._ep.clear_buffer(self._id)
        # Resolve any pending playout future (interrupt cancels the checkpoint wait)
        if self._plivo_playout_fut is not None and not self._plivo_playout_fut.done():
            self._plivo_playout_fut.set_result(None)
        self._plivo_playout_fut = None
