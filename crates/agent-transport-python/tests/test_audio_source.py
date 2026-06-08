"""Tests for TransportAudioSource — LiveKit-faithful FfiQueue pattern.

The audio source must:
- subscribe to the FfiQueue BEFORE calling send_audio_async (or
  wait_for_playout_async), so a synchronous Rust-side completion event
  doesn't disappear (closes the dispatch-before-wait race).
- await wait_for(predicate) to find the matching async_id event.
- unsubscribe in a finally block.
- raise RuntimeError when the event is audio_capture_error.
- raise asyncio.TimeoutError when no event arrives within 30s.
"""

import asyncio

import pytest

from agent_transport import _ffi_queue as _ffi
from agent_transport import _event as _evt
from agent_transport.sip.livekit import _audio_source as _src

FfiQueue = _ffi.FfiQueue
TransportAudioSource = _src.TransportAudioSource

FfiEvent = _evt.FfiEvent
CaptureAudioFrameCallback = _evt.CaptureAudioFrameCallback
TransportEvent = _evt.TransportEvent
PlayoutCompleteCallback = _evt.PlayoutCompleteCallback


class _FakeAudioFrame:
    """Minimal rtc.AudioFrame stand-in for unit tests."""

    def __init__(self, samples_per_channel=160, sample_rate=8000, num_channels=1):
        self.samples_per_channel = samples_per_channel
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.data = bytearray(samples_per_channel * 2)


class FakeEndpoint:
    """In-process endpoint stub for unit tests.

    ``send_audio_async`` / ``wait_for_playout_async`` allocate a
    monotonic async_id and emit the matching completion event into the
    associated FfiQueue. The ``emit_when`` flag controls whether the
    emit is synchronous (inside the FFI-equivalent call — the race
    scenario) or deferred (after a yield — the typical case).
    """

    SESSION_ID = "test-session"

    def __init__(self, events: FfiQueue, emit_when: str = "after"):
        assert emit_when in {"before_return", "after"}
        self._events = events
        self._next_id = 1
        self._emit_when = emit_when
        self.send_audio_calls = 0
        self.wait_for_playout_calls = 0
        self.clear_buffer_calls = 0
        self.queued_duration_ms_value = 0.0

    def _emit(self, event_type: str, async_id: int, error: str | None = None,
              session_id: str | None = None):
        """Emit a LiveKit-shape FfiEvent matching the production sink.

        Tests pass logical event types (``audio_capture_complete``,
        ``audio_capture_error``, ``audio_playout_complete``); we translate
        each to the appropriate FfiEvent variant so subscribers see what
        the real Rust dispatcher (via ``_build_ffi_events``) would
        produce.

        Note: ``audio_capture_error`` fans to BOTH ``capture_audio_frame``
        and ``transport_event.playout_complete`` variants (same async_id)
        because Rust can't tell from the dict which kind of awaiter is
        watching. Mirrors ``_event_sink._build_ffi_events``.
        """
        sid = session_id if session_id is not None else self.SESSION_ID
        err = error or ""
        if event_type == "audio_capture_complete":
            self._events.put(FfiEvent(
                capture_audio_frame=CaptureAudioFrameCallback(
                    async_id=async_id, error=err, source_handle=sid,
                )
            ))
        elif event_type == "audio_playout_complete":
            self._events.put(FfiEvent(
                transport_event=TransportEvent(
                    playout_complete=PlayoutCompleteCallback(
                        async_id=async_id, error=err, source_handle=sid,
                    )
                )
            ))
        elif event_type == "audio_capture_error":
            self._events.put(FfiEvent(
                capture_audio_frame=CaptureAudioFrameCallback(
                    async_id=async_id, error=err, source_handle=sid,
                )
            ))
            self._events.put(FfiEvent(
                transport_event=TransportEvent(
                    playout_complete=PlayoutCompleteCallback(
                        async_id=async_id, error=err, source_handle=sid,
                    )
                )
            ))
        else:
            raise AssertionError(f"unhandled event_type {event_type!r}")

    def send_audio_async(self, session_id, audio, sample_rate, num_channels) -> int:
        self.send_audio_calls += 1
        async_id = self._next_id
        self._next_id += 1
        if self._emit_when == "before_return":
            # Synchronous emit — race repro: event fires before this FFI
            # call has returned, before the caller has done wait_for.
            self._emit("audio_capture_complete", async_id)
        return async_id

    def wait_for_playout_async(self, session_id) -> int:
        self.wait_for_playout_calls += 1
        async_id = self._next_id
        self._next_id += 1
        if self._emit_when == "before_return":
            self._emit("audio_playout_complete", async_id)
        return async_id

    def clear_buffer(self, session_id):
        self.clear_buffer_calls += 1

    def queued_duration_ms(self, session_id):
        return self.queued_duration_ms_value


def _make_source(ep, events):
    return TransportAudioSource(
        endpoint=ep,
        call_or_session_id=FakeEndpoint.SESSION_ID,
        sample_rate=8000,
        num_channels=1,
        events=events,
    )


# ─── Subscribe-before-request race fix ───────────────────────────────────


@pytest.mark.asyncio
async def test_subscribe_before_request_closes_race_for_capture():
    """The race repro: Rust emits AudioCaptureComplete inside the FFI
    call, before the Python caller has registered any waiter. Under the
    pre-refactor EventWaiter, the event was dropped and the await hung
    to a 30s TimeoutError. Under the FfiQueue pattern, the subscribe
    happened BEFORE the FFI call so the event lands in the queue and
    wait_for finds it on the next yield."""
    events = FfiQueue()
    ep = FakeEndpoint(events, emit_when="before_return")
    src = _make_source(ep, events)
    # If the race were still live, this would block 30s and raise TimeoutError.
    await asyncio.wait_for(src.capture_frame(_FakeAudioFrame()), timeout=1.0)
    assert ep.send_audio_calls == 1


@pytest.mark.asyncio
async def test_subscribe_before_request_closes_race_for_playout():
    events = FfiQueue()
    ep = FakeEndpoint(events, emit_when="before_return")
    src = _make_source(ep, events)
    await asyncio.wait_for(src.wait_for_playout(), timeout=1.0)
    assert ep.wait_for_playout_calls == 1


# ─── Deferred-emit (typical) path ────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_frame_resolves_on_deferred_event():
    """Typical path: send_audio_async returns, then the event fires
    (e.g., from the Rust send-loop tick draining the buffer)."""
    events = FfiQueue()
    ep = FakeEndpoint(events, emit_when="after")
    src = _make_source(ep, events)

    async def fire_after_a_moment():
        # Wait long enough for src.capture_frame to subscribe + call FFI
        # + reach `await queue.wait_for`.
        await asyncio.sleep(0.05)
        ep._emit("audio_capture_complete", 1)

    fire_task = asyncio.create_task(fire_after_a_moment())
    await asyncio.wait_for(src.capture_frame(_FakeAudioFrame()), timeout=1.0)
    await fire_task


@pytest.mark.asyncio
async def test_wait_for_playout_resolves_on_deferred_event():
    events = FfiQueue()
    ep = FakeEndpoint(events, emit_when="after")
    src = _make_source(ep, events)

    async def fire_after_a_moment():
        await asyncio.sleep(0.05)
        ep._emit("audio_playout_complete", 1)

    fire_task = asyncio.create_task(fire_after_a_moment())
    await asyncio.wait_for(src.wait_for_playout(), timeout=1.0)
    await fire_task


# ─── Intermediate-events scan ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_intermediate_events_discarded_until_match():
    """Queue.wait_for must skip past unrelated events until predicate
    matches — the LiveKit `wait_for(fnc)` contract."""
    events = FfiQueue()
    ep = FakeEndpoint(events, emit_when="after")
    src = _make_source(ep, events)

    async def fire_noise_then_match():
        await asyncio.sleep(0.02)
        # Unrelated events from the same session (different async_ids)
        ep._emit("audio_capture_complete", 99)
        ep._emit("audio_playout_complete", 98)
        # The target — async_id 1 is what capture_frame is waiting for.
        ep._emit("audio_capture_complete", 1)

    fire_task = asyncio.create_task(fire_noise_then_match())
    await asyncio.wait_for(src.capture_frame(_FakeAudioFrame()), timeout=1.0)
    await fire_task


# ─── Error propagation ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_frame_raises_on_audio_capture_error():
    events = FfiQueue()
    ep = FakeEndpoint(events, emit_when="after")
    src = _make_source(ep, events)

    async def fire_error():
        await asyncio.sleep(0.02)
        ep._emit("audio_capture_error", 1, error="cleared")

    fire_task = asyncio.create_task(fire_error())
    with pytest.raises(RuntimeError, match="cleared"):
        await asyncio.wait_for(src.capture_frame(_FakeAudioFrame()), timeout=1.0)
    await fire_task


@pytest.mark.asyncio
async def test_wait_for_playout_raises_on_audio_capture_error():
    events = FfiQueue()
    ep = FakeEndpoint(events, emit_when="after")
    src = _make_source(ep, events)

    async def fire_error():
        await asyncio.sleep(0.02)
        ep._emit("audio_capture_error", 1, error="buffer_dropped")

    fire_task = asyncio.create_task(fire_error())
    with pytest.raises(RuntimeError, match="buffer_dropped"):
        await asyncio.wait_for(src.wait_for_playout(), timeout=1.0)
    await fire_task


# ─── Timeout / no-event path ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_frame_raises_on_timeout(monkeypatch):
    """If Rust never emits, wait_for must raise TimeoutError so the
    caller can recover instead of hanging forever."""
    # Patch the default timeout down for fast test. ``_src`` is the
    # directly-loaded _audio_source module (see _load_under_test
    # at the top of the file).
    monkeypatch.setattr(_src, "DEFAULT_WAIT_TIMEOUT", 0.05)

    events = FfiQueue()

    class _SilentEndpoint(FakeEndpoint):
        def send_audio_async(self, *args, **kwargs):
            self.send_audio_calls += 1
            async_id = self._next_id
            self._next_id += 1
            # Deliberately no emit.
            return async_id

    ep = _SilentEndpoint(events)
    src = _make_source(ep, events)
    with pytest.raises(asyncio.TimeoutError):
        await src.capture_frame(_FakeAudioFrame())


# ─── Unsubscribe-in-finally invariant ────────────────────────────────────


@pytest.mark.asyncio
async def test_unsubscribe_happens_when_send_audio_raises():
    """If the FFI call raises (e.g., session not active), the finally
    block must still unsubscribe — otherwise the FfiQueue leaks
    subscribers."""
    events = FfiQueue()

    class _RaisingEndpoint(FakeEndpoint):
        def send_audio_async(self, *args, **kwargs):
            raise RuntimeError("simulated session-not-active")

    ep = _RaisingEndpoint(events)
    src = _make_source(ep, events)
    before = events.subscriber_count()
    with pytest.raises(RuntimeError, match="simulated session-not-active"):
        await src.capture_frame(_FakeAudioFrame())
    assert events.subscriber_count() == before, (
        "subscribe-in-try / unsubscribe-in-finally invariant broken — "
        "FfiQueue subscribers leaked when FFI raised"
    )


@pytest.mark.asyncio
async def test_unsubscribe_happens_on_audio_capture_error():
    """When the event is audio_capture_error and wait_for_playout
    raises, the finally must still have unsubscribed."""
    events = FfiQueue()
    ep = FakeEndpoint(events, emit_when="after")
    src = _make_source(ep, events)

    async def fire_error():
        await asyncio.sleep(0.02)
        ep._emit("audio_capture_error", 1, error="cleared")

    fire_task = asyncio.create_task(fire_error())
    before = events.subscriber_count()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(src.wait_for_playout(), timeout=1.0)
    await fire_task
    assert events.subscriber_count() == before


# ─── Disposed source short-circuit ───────────────────────────────────────


@pytest.mark.asyncio
async def test_disposed_source_is_a_noop():
    events = FfiQueue()
    ep = FakeEndpoint(events)
    src = _make_source(ep, events)
    await src.aclose()
    # Should return without doing anything — no FFI call, no subscribe.
    await src.capture_frame(_FakeAudioFrame())
    assert ep.send_audio_calls == 0


# ─── Cross-session filtering ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_other_sessions_events_do_not_match():
    """The audio source's filter_fn must narrow to its own session_id;
    events from other sessions go into other subscribers' queues, not
    ours. This is the LiveKit-faithful "many subscribers, one broker"
    fan-out semantic."""
    events = FfiQueue()
    ep = FakeEndpoint(events, emit_when="after")
    src = _make_source(ep, events)

    async def fire_other_session_then_ours():
        await asyncio.sleep(0.02)
        # Different session — filter_fn must drop it at the broker.
        ep._emit("audio_capture_complete", 1, session_id="OTHER")
        # Our session, matching async_id.
        ep._emit("audio_capture_complete", 1)

    fire_task = asyncio.create_task(fire_other_session_then_ours())
    await asyncio.wait_for(src.capture_frame(_FakeAudioFrame()), timeout=1.0)
    await fire_task


# ─── TransportAudioSource (parity with parent) ─────────────────────────


@pytest.mark.asyncio
async def test_audio_stream_subclass_inherits_behavior():
    events = FfiQueue()
    ep = FakeEndpoint(events, emit_when="before_return")
    src = TransportAudioSource(
        endpoint=ep,
        call_or_session_id=FakeEndpoint.SESSION_ID,
        sample_rate=8000,
        num_channels=1,
        events=events,
    )
    await asyncio.wait_for(src.capture_frame(_FakeAudioFrame()), timeout=1.0)
    await asyncio.wait_for(src.wait_for_playout(), timeout=1.0)
