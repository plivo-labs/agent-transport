"""Tests for ``TransportAudioOutput`` — the Pattern A subclass of LiveKit's
``_ParticipantAudioOutput``.

Coverage areas:

1. ``__init__`` replaces ``self._audio_source`` with our Rust-backed
   ``TransportAudioSource`` after super().__init__.
2. ``_publish_track`` resolves ``_subscribed_fut`` immediately and is
   idempotent (second call doesn't raise).
3. ``capture_frame`` auto-starts ``_forwarding_task`` on first call.
4. ``pause``/``resume`` propagate to the Rust endpoint AND maintain the
   ``_rust_paused`` flag with rollback on endpoint failure.

These tests build the object via ``TransportAudioOutput.__new__`` and wire
just the fields they exercise — avoids constructing a real
``rtc.AudioSource`` (which allocates a libwebrtc FFI handle).
"""

from __future__ import annotations

import asyncio

import pytest


# ─── Constructor: source-swap behaviour ─────────────────────────────────────


def test_init_swaps_audio_source_for_transport_audio_source():
    """After ``super().__init__`` constructs an orphan ``rtc.AudioSource``,
    our override replaces it with ``TransportAudioSource``.
    """
    from agent_transport.sip.livekit._audio_io import TransportAudioOutput
    from agent_transport.sip.livekit._audio_source import TransportAudioSource

    class _FakeEp:
        input_sample_rate = 8000
        output_sample_rate = 8000

    out = TransportAudioOutput(_FakeEp(), "sid-test", sample_rate=8000, num_channels=1)
    try:
        assert isinstance(out._audio_source, TransportAudioSource)
        assert out._audio_source._id == "sid-test"
        assert out._audio_source.sample_rate == 8000
    finally:
        # _audio_source.aclose is a no-op (sets _disposed=True); the orphan
        # rtc.AudioSource the parent created has its own __del__ for handle
        # cleanup.
        pass


def test_init_records_endpoint_and_sid_and_paused_state():
    from agent_transport.sip.livekit._audio_io import TransportAudioOutput

    class _FakeEp:
        input_sample_rate = 8000
        output_sample_rate = 8000

    ep = _FakeEp()
    out = TransportAudioOutput(ep, "sid-test", sample_rate=8000, num_channels=1)
    assert out._ep is ep
    assert out._sid == "sid-test"
    assert out._rust_paused is False


# ─── _publish_track: idempotent, resolves immediately ──────────────────────


@pytest.mark.asyncio
async def test_publish_track_resolves_subscribed_fut():
    """LiveKit's ``_ParticipantAudioOutput.start`` does
    ``await self._publish_track()`` and only after that
    completes will ``capture_frame`` flow (parent's ``capture_frame`` awaits
    ``self._subscribed_fut``). Our override must resolve the future.
    """
    from agent_transport.sip.livekit._audio_io import TransportAudioOutput

    class _FakeEp:
        input_sample_rate = 8000
        output_sample_rate = 8000

    out = TransportAudioOutput(_FakeEp(), "sid-test", sample_rate=8000, num_channels=1)
    assert not out._subscribed_fut.done()

    await out._publish_track()
    assert out._subscribed_fut.done()
    assert out._subscribed_fut.result() is None


@pytest.mark.asyncio
async def test_publish_track_is_idempotent():
    """Calling ``_publish_track`` twice (LiveKit's reconnection path
    re-invokes it on the ``"reconnected"`` room event) must not raise
    ``InvalidStateError`` on the already-resolved future.
    """
    from agent_transport.sip.livekit._audio_io import TransportAudioOutput

    class _FakeEp:
        input_sample_rate = 8000
        output_sample_rate = 8000

    out = TransportAudioOutput(_FakeEp(), "sid-test", sample_rate=8000, num_channels=1)
    await out._publish_track()
    await out._publish_track()    # second call must not raise
    assert out._subscribed_fut.done()


# ─── capture_frame: auto-start guard ────────────────────────────────────────


@pytest.mark.asyncio
async def test_capture_frame_auto_starts_forwarding_task(monkeypatch):
    """LiveKit's ``RoomIO.start`` would normally call
    ``audio_output.start()`` which sets ``_forwarding_task``. We bypass
    RoomIO; our override must start it on first ``capture_frame``.
    """
    from agent_transport.sip.livekit._audio_io import TransportAudioOutput
    from livekit import rtc

    class _FakeEp:
        input_sample_rate = 8000
        output_sample_rate = 8000

    out = TransportAudioOutput(_FakeEp(), "sid-test", sample_rate=8000, num_channels=1)
    # Patch parent's capture_frame to a no-op coroutine so we don't actually
    # push into _audio_buf (which would need a working Chan).
    from livekit.agents.voice.room_io._output import _ParticipantAudioOutput

    capture_called = []

    async def fake_parent_capture(self, frame):
        capture_called.append(frame)

    monkeypatch.setattr(_ParticipantAudioOutput, "capture_frame", fake_parent_capture)

    # Resolve _subscribed_fut so parent's wait isn't load-bearing here.
    await out._publish_track()

    assert out._forwarding_task is None

    frame = rtc.AudioFrame(b"\x00\x00" * 160, sample_rate=8000, num_channels=1,
                           samples_per_channel=160)
    await out.capture_frame(frame)
    assert out._forwarding_task is not None
    assert len(capture_called) == 1

    # Cleanup the task we accidentally spawned (it'll be reading from an
    # empty _audio_buf forever otherwise).
    if out._forwarding_task and not out._forwarding_task.done():
        out._forwarding_task.cancel()
        try:
            await out._forwarding_task
        except (asyncio.CancelledError, Exception):
            pass


# ─── pause/resume: Rust propagation + rollback on failure ───────────────────


@pytest.mark.asyncio
async def test_pause_propagates_to_rust_endpoint():
    """When the agent pauses output, our override calls
    ``ep.pause(sid)`` AND sets ``_rust_paused = True``.
    """
    from agent_transport.sip.livekit._audio_io import TransportAudioOutput

    class _FakeEp:
        input_sample_rate = 8000
        output_sample_rate = 8000
        pause_calls: list = []

        def pause(self, sid):
            self.pause_calls.append(sid)

        def resume(self, sid):
            pass

    ep = _FakeEp()
    out = TransportAudioOutput(ep, "sid-test", sample_rate=8000, num_channels=1)
    out.pause()
    assert ep.pause_calls == ["sid-test"]
    assert out._rust_paused is True


@pytest.mark.asyncio
async def test_pause_failure_does_not_set_rust_paused():
    """Regression: if ``ep.pause`` raises, ``_rust_paused`` must stay
    False so the next ``pause()`` retries. (Pre-Phase-A behaviour set the
    flag eagerly and never recovered.)
    """
    from agent_transport.sip.livekit._audio_io import TransportAudioOutput

    class _FailingEp:
        input_sample_rate = 8000
        output_sample_rate = 8000

        def pause(self, sid):
            raise RuntimeError("simulated pause failure")

        def resume(self, sid):
            pass

    out = TransportAudioOutput(_FailingEp(), "sid-test", sample_rate=8000, num_channels=1)
    out.pause()
    assert out._rust_paused is False, (
        "ep.pause raised; _rust_paused must stay False so the next pause "
        "retries instead of being short-circuited"
    )


@pytest.mark.asyncio
async def test_pause_short_circuits_if_already_paused():
    """Calling ``pause`` twice in a row should only call Rust once."""
    from agent_transport.sip.livekit._audio_io import TransportAudioOutput

    class _FakeEp:
        input_sample_rate = 8000
        output_sample_rate = 8000
        pause_calls = 0

        def pause(self, sid):
            self.pause_calls += 1

        def resume(self, sid):
            pass

    ep = _FakeEp()
    out = TransportAudioOutput(ep, "sid-test", sample_rate=8000, num_channels=1)
    out.pause()
    out.pause()
    assert ep.pause_calls == 1


@pytest.mark.asyncio
async def test_resume_propagates_to_rust_and_clears_flag():
    from agent_transport.sip.livekit._audio_io import TransportAudioOutput

    class _FakeEp:
        input_sample_rate = 8000
        output_sample_rate = 8000
        resume_calls: list = []

        def pause(self, sid):
            pass

        def resume(self, sid):
            self.resume_calls.append(sid)

    ep = _FakeEp()
    out = TransportAudioOutput(ep, "sid-test", sample_rate=8000, num_channels=1)
    out._rust_paused = True   # simulate previously-paused state
    out.resume()
    assert ep.resume_calls == ["sid-test"]
    assert out._rust_paused is False


@pytest.mark.asyncio
async def test_resume_failure_keeps_rust_paused():
    """Mirror of pause-failure: if resume raises, ``_rust_paused`` must
    stay True so the next ``resume`` retries.
    """
    from agent_transport.sip.livekit._audio_io import TransportAudioOutput

    class _FailingResume:
        input_sample_rate = 8000
        output_sample_rate = 8000

        def pause(self, sid):
            pass

        def resume(self, sid):
            raise RuntimeError("simulated resume failure")

    out = TransportAudioOutput(_FailingResume(), "sid-test", sample_rate=8000, num_channels=1)
    out._rust_paused = True
    out.resume()
    assert out._rust_paused is True


# ─── _wait_for_playout override: child-task lifecycle fixes ─────────────────
#
# The parent's _wait_for_playout orphans both of its child tasks when the
# flush task is cancelled mid-``asyncio.wait`` (prod: "Task was destroyed but
# it is pending!" on the interruption waiter) and never retrieves the
# buffered-audio child's exception (prod: "Task exception was never
# retrieved" for the Rust playout wait's 30s TimeoutError). Our override is
# a verbatim copy plus a ``finally`` that reaps both children and retrieves
# the playout child's exception. These tests exercise those exact paths.


def _make_output_for_playout_tests():
    """TransportAudioOutput with a controllable fake audio source."""
    from agent_transport.sip.livekit._audio_io import TransportAudioOutput

    class _FakeEp:
        input_sample_rate = 8000
        output_sample_rate = 8000

    class _FakeSource:
        """Stands in for TransportAudioSource inside _wait_buffered_audio."""

        def __init__(self):
            self.playout_gate = asyncio.Event()
            self.playout_exc: BaseException | None = None
            self.queued_duration = 0.0
            self.cleared = False

        async def wait_for_playout(self):
            await self.playout_gate.wait()
            if self.playout_exc is not None:
                raise self.playout_exc

        def clear_queue(self):
            self.cleared = True

        async def aclose(self):
            pass

    out = TransportAudioOutput(_FakeEp(), "sid-test", sample_rate=8000, num_channels=1)
    src = _FakeSource()
    out._audio_source = src
    return out, src


@pytest.mark.asyncio
async def test_wait_for_playout_cancellation_reaps_children():
    """Cancelling the flush task mid-wait (aclose during teardown) must not
    leave either child task pending — the parent's version leaves the
    interruption waiter suspended on an Event that is never set."""
    out, src = _make_output_for_playout_tests()
    # Non-empty buffer so _wait_buffered_audio blocks on the playout gate.
    out._audio_buf.send_nowait(object())

    before = asyncio.all_tasks()
    flush = asyncio.create_task(out._wait_for_playout())
    await asyncio.sleep(0.01)  # let both children start and block

    flush.cancel()
    await asyncio.gather(flush, return_exceptions=True)
    await asyncio.sleep(0.01)  # let done-callbacks settle

    leaked = {
        t for t in asyncio.all_tasks() - before
        if not t.done() and t is not asyncio.current_task()
    }
    assert not leaked, f"child tasks leaked after cancellation: {leaked}"


@pytest.mark.asyncio
async def test_wait_for_playout_retrieves_playout_exception():
    """A playout-wait failure (the Rust 30s TimeoutError) must be retrieved
    and logged, not left as an unretrieved task exception — and the parent's
    on_playback_finished(interrupted=False) behavior must be preserved."""
    out, src = _make_output_for_playout_tests()
    out._audio_buf.send_nowait(object())
    out._pushed_duration = 1.5

    finished: list[tuple[float, bool]] = []
    out.on_playback_finished = lambda *, playback_position, interrupted, **kw: (
        finished.append((playback_position, interrupted))
    )

    src.playout_exc = TimeoutError("simulated 30s playout cap")
    src.playout_gate.set()

    unretrieved: list[str] = []
    loop = asyncio.get_running_loop()
    prev_handler = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda loop, ctx: unretrieved.append(ctx.get("message", ""))
    )
    try:
        await out._wait_for_playout()
        # Unretrieved-exception warnings fire from task GC — force the
        # window where they would surface.
        import gc

        gc.collect()
        await asyncio.sleep(0.05)
    finally:
        loop.set_exception_handler(prev_handler)

    assert finished == [(1.5, False)], (
        "on_playback_finished(interrupted=False) semantics must match the parent"
    )
    assert not unretrieved, f"unretrieved task exceptions: {unretrieved}"


@pytest.mark.asyncio
async def test_wait_for_playout_interrupted_path_matches_parent():
    """Verbatim-copy regression: the interrupted branch must still drain the
    buffer into queued_duration, clear the source queue, and report
    interrupted=True with the adjusted position."""
    out, src = _make_output_for_playout_tests()

    class _Frame:
        duration = 0.25

    out._audio_buf.send_nowait(_Frame())
    out._pushed_duration = 1.0
    src.queued_duration = 0.25

    finished: list[tuple[float, bool]] = []
    out.on_playback_finished = lambda *, playback_position, interrupted, **kw: (
        finished.append((playback_position, interrupted))
    )

    flush = asyncio.create_task(out._wait_for_playout())
    await asyncio.sleep(0.01)
    out._interrupted_event.set()
    await flush

    # 1.0 pushed - (0.25 source-queued + 0.25 buffered) = 0.5
    assert finished == [(0.5, True)]
    assert src.cleared is True
    assert not out._interrupted_event.is_set()
