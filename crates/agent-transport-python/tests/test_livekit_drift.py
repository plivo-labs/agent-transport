"""Drift detector for the Pattern A inheritance from LiveKit's
``_ParticipantAudioOutput``.

Our ``TransportAudioOutput`` subclasses LiveKit's
``livekit.agents.voice.room_io._output._ParticipantAudioOutput`` and overrides
five methods (``__init__``, ``_publish_track``, ``capture_frame``,
``pause``/``resume``, and ``_wait_for_playout`` — the last a verbatim copy
plus child-task lifecycle fixes, pinned in section 4b). Everything else —
``_forward_audio``, ``flush``, ``clear_buffer``, and the
``_pushed_duration`` / ``_interrupted_event`` / ``_first_frame_event`` /
``_playback_enabled`` / ``_audio_buf`` / ``_flush_task`` / ``_forwarding_task``
state machine — is **inherited verbatim**.

A LiveKit minor release can rename a private attribute or move a guard
condition out of a method. If that happens silently, our subclass keeps
loading and runs into ``AttributeError`` at the worst possible moment
(mid-call). This file pins the assumptions we depend on.

Pair with the ``livekit-agents>=1.5,<2`` upper bound in ``pyproject.toml``
— bump them together when validating a new LiveKit major.
"""

from __future__ import annotations

import inspect
import asyncio

import pytest


# ─── Imports the base class up-front so test_collection fails loud ──────────

from livekit.agents.voice.room_io._output import _ParticipantAudioOutput  # noqa: E402

from agent_transport.sip.livekit._audio_io import TransportAudioOutput  # noqa: E402


# ─── 1. Constructor signature ───────────────────────────────────────────────


def test_base_init_accepts_our_kwargs():
    """``TransportAudioOutput.__init__`` calls ``super().__init__`` with:

        room=_FakeRoom,
        sample_rate=int,
        num_channels=int,
        track_publish_options=rtc.TrackPublishOptions(),

    Verify the base accepts exactly those kwargs (any extras would be a
    new required arg we're not passing — silent fail).
    """
    sig = inspect.signature(_ParticipantAudioOutput.__init__)
    params = dict(sig.parameters)

    # We pass these positional / keyword args. They MUST exist on the
    # base class with these names.
    for required in ("room", "sample_rate", "num_channels", "track_publish_options"):
        assert required in params, (
            f"_ParticipantAudioOutput.__init__ is missing the {required!r} "
            f"parameter — Pattern A inheritance will fail at construction"
        )

    # No NEW required positional arg should sneak in. We pass 4 positional
    # via kwargs; anything that became *required* (no default) and isn't in
    # the list above would break us.
    required_no_default = [
        name for name, p in params.items()
        if name != "self"
        and p.default is inspect.Parameter.empty
        and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    known = {"self", "room", "sample_rate", "num_channels", "track_publish_options"}
    surprises = set(required_no_default) - known
    assert not surprises, (
        f"_ParticipantAudioOutput.__init__ has a new required arg we don't "
        f"pass: {surprises!r}. TransportAudioOutput.__init__ must be updated."
    )


# ─── 2. Instance attribute set ──────────────────────────────────────────────


# Attributes our subclass and the parent's inherited methods depend on by
# name. If LiveKit renames any of these, our code raises AttributeError
# mid-call. Pin them here.
_REQUIRED_INSTANCE_ATTRS = (
    # We override _audio_source post-super; the base must have set it.
    "_audio_source",
    # _publish_track uses these via async-with-lock.
    "_lock",
    "_subscribed_fut",
    "_publication",
    "_publish_options",
    # Our capture_frame override checks _forwarding_task.
    "_forwarding_task",
    # _forward_audio (inherited) reads/writes these.
    "_audio_buf",
    "_audio_bstream",
    "_pushed_duration",
    "_interrupted_event",
    "_first_frame_event",
    "_playback_enabled",
    "_flush_task",
    # _publish_track + aclose touch this.
    "_room",
    # NOTE: ``_republish_task`` and the ``_on_reconnected`` listener path
    # were removed by LiveKit in 1.5.9. Our ``_FakeRoom`` still exposes
    # ``on``/``off`` for forward-compat in case the listener returns; the
    # base no longer registers it.
)


def test_base_init_sets_expected_instance_attrs():
    """After ``__init__``, the base must have all the attributes our code
    (subclass overrides + inherited methods we rely on) accesses by name.
    """
    # Construct without invoking our subclass override (which swaps
    # _audio_source). We only want to check what the BASE sets up.
    from livekit import rtc
    from agent_transport.sip.livekit._fake_room import _FakeRoom

    base = _ParticipantAudioOutput(
        room=_FakeRoom(identity="drift-test"),
        sample_rate=8000,
        num_channels=1,
        track_publish_options=rtc.TrackPublishOptions(),
    )
    try:
        missing = [name for name in _REQUIRED_INSTANCE_ATTRS if not hasattr(base, name)]
        assert not missing, (
            f"_ParticipantAudioOutput instance is missing expected attrs: "
            f"{missing!r}. Our subclass / inherited methods will fail at "
            f"runtime with AttributeError on these."
        )
    finally:
        # Best-effort: release the orphan rtc.AudioSource handle.
        try:
            asyncio.get_event_loop().run_until_complete(base.aclose())
        except Exception:
            pass


# ─── 3. _publish_track is still async (we override it) ──────────────────────


def test_publish_track_is_async():
    """Our override returns a coroutine. If the base became sync, our
    override would silently shadow it and our `await self._publish_track()`
    callsite would fail.
    """
    assert asyncio.iscoroutinefunction(_ParticipantAudioOutput._publish_track), (
        "_ParticipantAudioOutput._publish_track is no longer an async def. "
        "Our TransportAudioOutput._publish_track override must match."
    )


# ─── 4. _forward_audio guard expression ─────────────────────────────────────


def test_forward_audio_has_stale_frame_guard():
    """The ``_pushed_duration == 0`` guard in ``_forward_audio`` is what
    keeps stale frames from a finalized speech handle out of the next
    turn's playout. We inherit this method verbatim; a LiveKit upgrade that
    refactors it differently (different guard expression, different
    variable name) silently breaks our stale-frame-skip contract.
    """
    src = inspect.getsource(_ParticipantAudioOutput._forward_audio)
    needle = "self._interrupted_event.is_set() or self._pushed_duration == 0"
    assert needle in src, (
        f"_ParticipantAudioOutput._forward_audio no longer contains the "
        f"stale-frame guard {needle!r}. Either LiveKit moved the guard "
        f"(verify it's still correct) or our `_pushed_duration == 0` "
        f"assumption is broken."
    )


# ─── 4b. _wait_for_playout source pin (we override with a fixed copy) ───────


_WAIT_FOR_PLAYOUT_EXPECTED_SRC = """\
    async def _wait_for_playout(self) -> None:
        wait_for_interruption = asyncio.create_task(self._interrupted_event.wait())

        async def _wait_buffered_audio() -> None:
            while not self._audio_buf.empty():
                if not self._playback_enabled.is_set():
                    await self._playback_enabled.wait()

                await self._audio_source.wait_for_playout()
                # avoid deadlock when clear_buffer called before capture_frame
                await asyncio.sleep(0)

        wait_for_playout = asyncio.create_task(_wait_buffered_audio())
        await asyncio.wait(
            [wait_for_playout, wait_for_interruption],
            return_when=asyncio.FIRST_COMPLETED,
        )

        interrupted = self._interrupted_event.is_set()
        pushed_duration = self._pushed_duration

        if interrupted:
            queued_duration = self._audio_source.queued_duration
            while not self._audio_buf.empty():
                queued_duration += self._audio_buf.recv_nowait().duration

            pushed_duration = max(pushed_duration - queued_duration, 0)
            self._audio_source.clear_queue()
            wait_for_playout.cancel()
        else:
            wait_for_interruption.cancel()

        self._pushed_duration = 0
        self._interrupted_event.clear()
        self._first_frame_event.clear()
        self.on_playback_finished(playback_position=pushed_duration, interrupted=interrupted)
"""


def test_wait_for_playout_source_matches_our_copy_baseline():
    """``TransportAudioOutput._wait_for_playout`` is a VERBATIM COPY of the
    parent's (plus two child-task lifecycle fixes: reap both children in a
    ``finally``, retrieve the buffered-audio child's exception). If upstream
    changes the original in any way — even a comment — this pin fails, which
    is the signal to re-sync our copy in ``_audio_io.py`` with the new
    upstream body (keeping the two fixes) and update this baseline.
    """
    src = inspect.getsource(_ParticipantAudioOutput._wait_for_playout)
    assert src == _WAIT_FOR_PLAYOUT_EXPECTED_SRC, (
        "_ParticipantAudioOutput._wait_for_playout changed upstream. "
        "Re-sync the fixed copy in TransportAudioOutput._wait_for_playout "
        "(_audio_io.py) against the new source — preserve the finally-block "
        "child reaping and exception retrieval — then update "
        "_WAIT_FOR_PLAYOUT_EXPECTED_SRC in this test."
    )


def test_our_wait_for_playout_override_present():
    """Guard against an accidental removal of the override (e.g. a rebase
    dropping it) — the parent's version orphans its child tasks when the
    flush task is cancelled and leaks the 30s playout TimeoutError as
    "Task exception was never retrieved"."""
    assert "_wait_for_playout" in TransportAudioOutput.__dict__, (
        "TransportAudioOutput no longer overrides _wait_for_playout — the "
        "child-task lifecycle fixes are gone."
    )


# ─── 5. _forward_audio + _wait_for_playout are async ────────────────────────


@pytest.mark.parametrize("method_name", [
    "_forward_audio",
    "_wait_for_playout",
    "capture_frame",
    "aclose",
    "start",
])
def test_inherited_method_is_async(method_name: str):
    """Methods we depend on being awaitable. If any becomes sync, our
    overrides + Pattern A flow break.
    """
    method = getattr(_ParticipantAudioOutput, method_name)
    assert asyncio.iscoroutinefunction(method), (
        f"_ParticipantAudioOutput.{method_name} is no longer an async def."
    )


# ─── 6. flush, clear_buffer, pause, resume are sync (we inherit them) ───────


@pytest.mark.parametrize("method_name", [
    "flush",
    "clear_buffer",
    "pause",
    "resume",
])
def test_inherited_method_is_sync(method_name: str):
    """These methods are sync in LiveKit's base. Our overrides
    (pause/resume) preserve that, and the others we inherit. A LiveKit
    change that makes them async would silently break callers that
    invoke them without ``await``.
    """
    method = getattr(_ParticipantAudioOutput, method_name)
    assert not asyncio.iscoroutinefunction(method), (
        f"_ParticipantAudioOutput.{method_name} became async in this LiveKit "
        f"version. Audit all callers (TransportAudioOutput overrides + "
        f"AgentSession internals)."
    )


# ─── 7. Our subclass MRO ────────────────────────────────────────────────────


def test_transport_audio_output_mro_includes_base():
    """Sanity check that Pattern A inheritance is intact — our class
    actually subclasses LiveKit's, not some shim of it.
    """
    assert _ParticipantAudioOutput in TransportAudioOutput.__mro__, (
        "TransportAudioOutput no longer inherits from "
        "_ParticipantAudioOutput. Pattern A is broken."
    )


# ─── 8. We override exactly the methods we documented ───────────────────────


def test_we_override_only_the_documented_methods():
    """Pattern A discipline: keep the override surface tiny so each
    LiveKit upgrade has minimal areas to re-validate. If this test fails
    because someone added an override, update the list AND the audit
    docs.
    """
    expected_overrides = {
        "__init__",
        "_publish_track",
        "capture_frame",
        "pause",
        "resume",
        "_wait_for_playout",  # fixed verbatim copy — pinned in section 4b
        "send_raw_message",   # our extension, not in base — also OK
        "__repr__",
    }
    # ``_abc_impl`` is auto-added by Python's ABC machinery on every
    # concrete subclass — not an actual override we wrote.
    actual = {
        name for name in TransportAudioOutput.__dict__
        if (not name.startswith("__") or name == "__init__" or name == "__repr__")
        and name != "_abc_impl"
    }
    overrides_of_base = actual & set(dir(_ParticipantAudioOutput))
    surprises = overrides_of_base - expected_overrides
    assert not surprises, (
        f"TransportAudioOutput overrides {surprises!r} on top of "
        f"_ParticipantAudioOutput that weren't documented. Update the "
        f"audit before landing — every override is a re-validation surface."
    )
