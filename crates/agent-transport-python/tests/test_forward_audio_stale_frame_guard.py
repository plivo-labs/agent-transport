"""Regression test for the `_forward_audio` stale-frame guard.

Mirrors upstream ``_ParticipantAudioOutput._forward_audio``
(``livekit/agents/voice/room_io/_output.py``), which gates frame
forwarding on BOTH ``_interrupted_event.is_set()`` AND
``_pushed_duration == 0``.

The ``_pushed_duration == 0`` branch protects against a specific race:

1. Speech handle 1 runs to completion: ``_pushed_duration`` accumulates
   while frames are captured, then ``_wait_for_playout`` resets it
   and clears ``_interrupted_event``.
2. A stale frame for speech handle 1 was still sitting in ``_audio_buf``
   when the reset happened.
3. Without the ``_pushed_duration == 0`` check, ``_forward_audio`` would
   wake up, see ``_interrupted_event`` cleared, and **replay the stale
   frame** as if it belonged to the next turn.
4. With the check, ``_forward_audio`` skips it because
   ``_pushed_duration`` is still zero (next turn hasn't started yet).

Post-Tier-B (Pattern A inheritance), :class:`TransportAudioOutput`
inherits ``_forward_audio`` directly from
``_ParticipantAudioOutput`` — so the guard is automatically applied. This
test pins both the runtime behavior and the source-level guard
expression to detect upstream drift (next LiveKit upgrade) that would
silently regress the safety net.
"""

import asyncio
import inspect

import pytest
from livekit import rtc

from agent_transport.sip.livekit._audio_io import TransportAudioOutput


class _RecordingAudioSource:
    """Stub AudioSource that records every frame passed to capture_frame()."""

    def __init__(self):
        self.captured: list[rtc.AudioFrame] = []
        self.sample_rate = 8000
        self.num_channels = 1
        self.queued_duration = 0.0

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        self.captured.append(frame)

    def clear_queue(self) -> None:
        pass

    async def wait_for_playout(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


def _make_frame(samples: int = 160) -> rtc.AudioFrame:
    return rtc.AudioFrame(
        data=b"\x01\x00" * samples,
        sample_rate=8000,
        num_channels=1,
        samples_per_channel=samples,
    )


def _bypass_init() -> TransportAudioOutput:
    """Construct a TransportAudioOutput without invoking
    ``_ParticipantAudioOutput.__init__`` (which would allocate an FFI
    handle for its orphan ``rtc.AudioSource``). Wire only the fields
    ``_forward_audio`` touches.
    """
    from livekit.agents.utils.aio import Chan as _LkChan

    t = TransportAudioOutput.__new__(TransportAudioOutput)
    t._audio_source = _RecordingAudioSource()
    # LiveKit's _forward_audio uses ``async for frame in self._audio_buf``
    # — must be ``utils.aio.Chan`` (LiveKit's), not our internal Chan.
    t._audio_buf = _LkChan()
    t._playback_enabled = asyncio.Event()
    t._playback_enabled.set()
    t._interrupted_event = asyncio.Event()
    t._first_frame_event = asyncio.Event()
    t._flush_task = None
    t._pushed_duration = 0.0
    t.on_playback_started = lambda *a, **kw: None
    t.on_playback_finished = lambda *a, **kw: None
    return t


# ─── Tests ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_frame_when_pushed_duration_is_zero():
    """``_forward_audio`` must skip frames while ``_pushed_duration == 0``
    — they belong to a speech handle that has already been finalized
    (its ``_wait_for_playout`` reset the field).
    """
    t = _bypass_init()
    t._audio_buf.send_nowait(_make_frame())

    task = asyncio.create_task(t._forward_audio())

    for _ in range(10):
        await asyncio.sleep(0.005)
        if t._audio_buf.empty():
            break

    assert t._audio_source.captured == [], (
        "Stale frame should be skipped while _pushed_duration == 0"
    )

    # Next speech turn begins: _pushed_duration becomes non-zero.
    t._pushed_duration = 0.02
    t._audio_buf.send_nowait(_make_frame())
    for _ in range(10):
        await asyncio.sleep(0.005)
        if len(t._audio_source.captured) > 0:
            break

    assert len(t._audio_source.captured) == 1, (
        "Frame should be forwarded once _pushed_duration is non-zero"
    )

    t._audio_buf.close()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_skips_frame_while_interrupted():
    """The other branch of the guard: frames are also skipped while
    ``_interrupted_event`` is set. Keep working alongside the
    pushed_duration guard.
    """
    t = _bypass_init()
    t._pushed_duration = 0.02
    t._interrupted_event.set()
    t._audio_buf.send_nowait(_make_frame())

    task = asyncio.create_task(t._forward_audio())
    for _ in range(10):
        await asyncio.sleep(0.005)
        if t._audio_buf.empty():
            break

    assert t._audio_source.captured == [], "Interrupted frames must be skipped"

    t._audio_buf.close()
    await asyncio.wait_for(task, timeout=1.0)


def test_inherited_forward_audio_has_guard():
    """Drift detector: source-level grep on the inherited ``_forward_audio``
    confirms the guard expression is still present in LiveKit's base
    class. Failing this is the canary that LiveKit's
    ``_ParticipantAudioOutput`` has changed shape — review needed.
    """
    src = inspect.getsource(TransportAudioOutput._forward_audio)
    needle = "self._interrupted_event.is_set() or self._pushed_duration == 0"
    assert needle in src, (
        "_ParticipantAudioOutput._forward_audio missing the stale-frame "
        f"guard {needle!r} — LiveKit base class has drifted"
    )
