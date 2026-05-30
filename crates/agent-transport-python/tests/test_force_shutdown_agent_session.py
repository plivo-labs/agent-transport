"""Unit tests for agent_transport.sip.livekit._session_teardown.

Covers the race-fix helper for issue #83. The helper is called from
``call_terminated`` handlers to tear down a LiveKit ``AgentSession``
synchronously so a buffered STT transcript delivered after disconnect
doesn't trigger a wasted LLM + TTS call on a dead session.
"""

import asyncio
from unittest.mock import MagicMock, AsyncMock

import pytest

from agent_transport.sip.livekit._session_teardown import (
    force_shutdown_agent_session,
)


def _make_session(*, with_audio_in=True, with_audio_out=True, shutdown_raises=False):
    """Build a minimal mock matching the LiveKit AgentSession shape we rely on."""
    session = MagicMock()
    session._closing = False
    session._activity = MagicMock()
    session._activity._scheduling_paused = False
    session._next_activity = MagicMock()
    session._next_activity._scheduling_paused = False

    if with_audio_in:
        session.input.audio = MagicMock()
        session.input.audio.aclose = AsyncMock()
    else:
        session.input.audio = None

    if with_audio_out:
        session.output.audio = MagicMock()
        session.output.audio.clear_buffer = MagicMock()
    else:
        session.output.audio = None

    if shutdown_raises:
        session.shutdown = MagicMock(side_effect=RuntimeError("boom"))
    else:
        session.shutdown = MagicMock()

    return session


def test_none_session_is_noop():
    """Helper must tolerate None (ctx._session may be unset when a call
    terminates before the entrypoint ran)."""
    tasks: set[asyncio.Task] = set()
    force_shutdown_agent_session(None, tasks)  # must not raise
    assert tasks == set()


async def test_sets_closing_flag_synchronously():
    """The core race fix: ``_closing`` must flip BEFORE we yield to the
    event loop, so buffered STT transcripts are dropped."""
    session = _make_session()
    tasks: set[asyncio.Task] = set()

    force_shutdown_agent_session(session, tasks)

    assert session._closing is True


async def test_pauses_activity_scheduling_before_shutdown():
    """The real LLM/TTS guard must flip before shutdown can yield."""
    session = _make_session()
    tasks: set[asyncio.Task] = set()
    observed_at_shutdown = []
    session.shutdown = MagicMock(
        side_effect=lambda **_: observed_at_shutdown.append(
            (
                session._activity._scheduling_paused,
                session._next_activity._scheduling_paused,
            )
        )
    )

    force_shutdown_agent_session(session, tasks)

    assert session._activity._scheduling_paused is True
    assert session._next_activity._scheduling_paused is True
    assert observed_at_shutdown == [(True, True)]


async def test_schedules_audio_input_aclose():
    """Audio input is closed as a task so STT stops getting new audio.
    The task lives in background_tasks so GC can't collect it mid-run."""
    session = _make_session()
    tasks: set[asyncio.Task] = set()

    force_shutdown_agent_session(session, tasks)

    assert len(tasks) == 1  # aclose task scheduled and tracked
    await asyncio.gather(*tasks)
    session.input.audio.aclose.assert_awaited_once()
    # Discard callback ran, so the set is empty after completion.
    assert tasks == set()


async def test_clears_audio_output_buffer():
    session = _make_session()
    tasks: set[asyncio.Task] = set()

    force_shutdown_agent_session(session, tasks)

    session.output.audio.clear_buffer.assert_called_once()


async def test_calls_shutdown_with_drain_false():
    """``drain=False`` force-interrupts in-flight speech — critical for the
    race fix; ``drain=True`` would wait for TTS to finish on a dead call."""
    session = _make_session()
    tasks: set[asyncio.Task] = set()

    force_shutdown_agent_session(session, tasks)

    session.shutdown.assert_called_once_with(drain=False)


async def test_tolerates_missing_audio_input():
    """Some session configurations have no audio input (e.g., text-only).
    Helper must not crash."""
    session = _make_session(with_audio_in=False)
    tasks: set[asyncio.Task] = set()

    force_shutdown_agent_session(session, tasks)

    assert tasks == set()  # nothing scheduled
    session.shutdown.assert_called_once()


async def test_tolerates_missing_audio_output():
    session = _make_session(with_audio_out=False)
    tasks: set[asyncio.Task] = set()

    force_shutdown_agent_session(session, tasks)

    session.shutdown.assert_called_once()


async def test_tolerates_shutdown_exception():
    """If ``session.shutdown()`` raises (e.g., session already closed),
    the helper must swallow it so the rest of call_terminated continues."""
    session = _make_session(shutdown_raises=True)
    tasks: set[asyncio.Task] = set()

    # Must not raise — this call is on the hot path of the event loop.
    force_shutdown_agent_session(session, tasks)

    assert session._closing is True  # still set, even though shutdown raised
