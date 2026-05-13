"""TransportAudioSource — drop-in equivalent of ``rtc.AudioSource``
for both our SIP/RTP and Plivo audio_stream transports.

LiveKit-faithful FFI pattern (``livekit/rtc/audio_source.py:135-149``):

    queue = FfiClient.instance.queue.subscribe(filter_fn=...)
    try:
        async_id = ep.send_audio_async(...)
        ev = await queue.wait_for(predicate)
    finally:
        FfiClient.instance.queue.unsubscribe(queue)

The subscribe happens BEFORE the FFI call. Even if the completion
event fires synchronously inside ``send_audio_async`` (Rust's
immediate-emit / below-threshold path), it lands in the subscribed
``Queue`` and ``wait_for`` finds it. This closes the dispatch-before-wait
race that the pre-refactor ``EventWaiter`` had.

Events arrive as LiveKit-shape :class:`FfiEvent` objects placed on the
process-global :data:`agent_transport._ffi_queue.GLOBAL` queue by the
constrained Rust dispatcher thread (see ``agent_transport._event_sink``).
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from typing import Any

from livekit import rtc

from agent_transport._event import FfiEvent
from agent_transport._ffi_queue import DEFAULT_WAIT_TIMEOUT, FfiQueue, Queue, GLOBAL

logger = logging.getLogger(__name__)


class TransportAudioSource:
    """Audio source that sends frames to a SIP/RTP or AudioStream endpoint.

    Matches rtc.AudioSource's backpressure and playout semantics exactly,
    using LiveKit's ``subscribe-before-request`` FFI pattern.
    """

    def __init__(
        self,
        endpoint: Any,
        call_or_session_id: str,
        sample_rate: int,
        num_channels: int = 1,
        queue_size_ms: int = 1000,
        loop: asyncio.AbstractEventLoop | None = None,
        events: FfiQueue | None = None,
    ) -> None:
        self._ep = endpoint
        self._id = call_or_session_id
        self._sample_rate = sample_rate
        self._num_channels = num_channels
        self._queue_size_ms = queue_size_ms
        self._loop = loop or asyncio.get_event_loop()
        self._disposed = False
        # Default: process-global FfiQueue (mirror of LiveKit's
        # ``FfiClient.instance.queue``). Tests can pass a private queue
        # to avoid cross-talk with other test cases.
        self._events: FfiQueue = events if events is not None else GLOBAL

    @property
    def events(self) -> FfiQueue:
        """Expose the FfiQueue we subscribe through (read-only)."""
        return self._events

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def num_channels(self) -> int:
        return self._num_channels

    @property
    def queued_duration(self) -> float:
        """Current duration (in seconds) of audio data queued for playback."""
        try:
            return self._ep.queued_duration_ms(self._id) / 1000.0
        except Exception:
            return 0.0

    def clear_queue(self) -> None:
        """Clear the queue immediately. Matches ``rtc.AudioSource.clear_queue``.

        Rust emits ``audio_capture_error { error: "cleared" }`` (translated to
        a ``capture_audio_frame`` FfiEvent with ``error="cleared"``) for every
        pending ``async_id``. Any in-flight ``capture_frame`` / ``wait_for_playout``
        will see that error on its ``wait_for`` and raise ``RuntimeError``.
        """
        self._ep.clear_buffer(self._id)

    # ─── FfiEvent predicates ─────────────────────────────────────────────────

    def _capture_filter(self, e: FfiEvent) -> bool:
        """filter_fn for ``capture_frame``: narrow GLOBAL to our session's
        ``capture_audio_frame`` events. Cheaper than dispatching every
        FfiEvent to every audio source's queue and letting ``wait_for``
        discard non-matches."""
        if e.WhichOneof("message") != "capture_audio_frame":
            return False
        return e.capture_audio_frame.source_handle == self._id

    def _playout_filter(self, e: FfiEvent) -> bool:
        """filter_fn for ``wait_for_playout``: narrow GLOBAL to our session's
        ``transport_event.playout_complete`` events.
        """
        if e.WhichOneof("message") != "transport_event":
            return False
        if e.transport_event.WhichOneof("event") != "playout_complete":
            return False
        return e.transport_event.playout_complete.source_handle == self._id

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        """Capture a frame and send it to the Rust transport layer.

        Always awaits the per-frame ``CaptureAudioFrameCallback`` from the
        FfiQueue, mirroring LiveKit's contract that every
        ``capture_audio_frame`` round-trips. Subscribes BEFORE the FFI
        call so a synchronously-emitted completion is captured in the
        queue regardless of GIL scheduling.

        Raises:
            RuntimeError: cleared / flushed / dropped while in-flight
                (delivered as a ``capture_audio_frame.error`` field).
            asyncio.TimeoutError: backpressure did not clear within
                ``DEFAULT_WAIT_TIMEOUT`` (30s).
        """
        # Matches `rtc.AudioSource.capture_frame:119` — silent early-return.
        # LiveKit does not log on the dispose / empty-frame skip path.
        if self._disposed or frame.samples_per_channel == 0:
            return

        queue: Queue[FfiEvent] = self._events.subscribe(
            loop=self._loop,
            filter_fn=self._capture_filter,
        )
        try:
            async_id = self._ep.send_audio_async(
                self._id,
                bytes(frame.data),
                frame.sample_rate,
                frame.num_channels,
            )
            # LiveKit-faithful: silent await on the capture callback. Their
            # `rtc.AudioSource.capture_frame` likewise just awaits the FfiEvent
            # without any per-frame instrumentation — every push waits a frame's
            # worth of drain time during sustained backpressure, that's the
            # designed semantics, not pathology. We previously logged a WARNING
            # at >5 ms which produced 220 noise lines per call; removed for
            # parity with `livekit/rtc/audio_source.py:135-149`.
            ev: FfiEvent = await queue.wait_for(
                lambda e: e.capture_audio_frame.async_id == async_id,
                timeout=DEFAULT_WAIT_TIMEOUT,
            )
        finally:
            self._events.unsubscribe(queue)

        if ev.capture_audio_frame.error:
            raise RuntimeError(
                f"capture_frame async_id={ev.capture_audio_frame.async_id} "
                f"failed: {ev.capture_audio_frame.error}"
            )

    async def wait_for_playout(self) -> None:
        """Wait for all queued audio to finish playing out.

        Same subscribe-before-request pattern. The Rust side always emits
        ``AudioPlayoutComplete`` (immediately if buffer empty, deferred
        otherwise), translated by the event sink to
        ``transport_event.playout_complete``.

        Raises:
            RuntimeError: cleared / flushed / dropped while waiting.
            asyncio.TimeoutError: did not drain within 30s.
        """
        if self._disposed:
            return

        queue: Queue[FfiEvent] = self._events.subscribe(
            loop=self._loop,
            filter_fn=self._playout_filter,
        )
        try:
            async_id = self._ep.wait_for_playout_async(self._id)
            ev: FfiEvent = await queue.wait_for(
                lambda e: e.transport_event.playout_complete.async_id == async_id,
                timeout=DEFAULT_WAIT_TIMEOUT,
            )
        finally:
            self._events.unsubscribe(queue)

        if ev.transport_event.playout_complete.error:
            raise RuntimeError(
                f"wait_for_playout async_id={ev.transport_event.playout_complete.async_id} "
                f"failed: {ev.transport_event.playout_complete.error}"
            )

    async def aclose(self) -> None:
        """Close the audio source.

        The FfiQueue is process-global and shared across all sessions —
        teardown is not our concern. Rust's ``AudioBuffer::Drop`` emits
        ``audio_capture_error`` for every pending ``async_id`` on session
        teardown, which the sink translates and any in-flight await sees
        as a ``RuntimeError``.
        """
        self._disposed = True


