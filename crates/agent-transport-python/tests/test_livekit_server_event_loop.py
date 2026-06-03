"""Regression tests for the livekit server lifecycle loop.

Post-Tier-A, both ``audio_stream_server.py`` and ``server.py`` consume
``FfiEvent`` objects off the process-global ``FfiQueue`` (mirroring
LiveKit's ``FfiClient.instance.queue`` pattern). The Rust dispatcher
thread spawned by ``set_event_sink`` translates each ``EndpointEvent``
into the corresponding ``FfiEvent`` and ``GLOBAL.put``s on the asyncio
loop. ``_lifecycle_loop`` subscribes to ``GLOBAL`` with a filter
narrowing to room/transport events.

The tests below run ``_lifecycle_loop`` against a swapped-in ``FfiQueue``
(via ``_ffi_queue.set_global``), inject FfiEvents directly, and verify
the dispatcher keeps running even when one event's handler raises.
"""

import asyncio
import pytest

from agent_transport import _ffi_queue as _ffi
from agent_transport._event import (
    FfiEvent,
    ParticipantConnected,
    ParticipantDisconnected,
    ParticipantInfo,
    RoomEvent,
)


class FakeEndpoint:
    """Minimal endpoint stand-in for the lifecycle-loop tests.

    Real ``_lifecycle_loop`` only calls ``ep.clear_buffer`` on
    ``participant_disconnected``; everything else flows through GLOBAL.
    """

    def __init__(self):
        self.clear_buffer_calls = 0

    def clear_buffer(self, session_id):
        self.clear_buffer_calls += 1


@pytest.fixture
def isolated_global():
    """Swap GLOBAL for a fresh FfiQueue so the lifecycle loop reads only
    events the test pushed. Restored on teardown."""
    saved = _ffi.get_global()
    fresh = _ffi.FfiQueue()
    _ffi.set_global(fresh)
    # The audio_stream_server / server import GLOBAL at module load,
    # so we need to re-bind their module-level reference too.
    from agent_transport.sip.livekit import audio_stream_server, server
    audio_stream_server.GLOBAL = fresh
    server.GLOBAL = fresh
    yield fresh
    _ffi.set_global(saved)
    audio_stream_server.GLOBAL = saved
    server.GLOBAL = saved


def _ev_participant_connected(session_id: str) -> FfiEvent:
    return FfiEvent(
        room_event=RoomEvent(
            room_handle=session_id,
            participant_connected=ParticipantConnected(
                info=ParticipantInfo(
                    identity=f"sip:caller-{session_id}@x",
                    session_id=session_id,
                    stream_id=f"stream-{session_id}",
                ),
            ),
        )
    )


def _ev_participant_disconnected(session_id: str) -> FfiEvent:
    return FfiEvent(
        room_event=RoomEvent(
            room_handle=session_id,
            participant_disconnected=ParticipantDisconnected(
                participant_identity=f"sip:caller-{session_id}@x",
                disconnect_reason=1,
                session_id=session_id,
                reason="test",
            ),
        )
    )


@pytest.mark.asyncio
async def test_audio_stream_server_lifecycle_loop_survives_handler_exception(isolated_global):
    """Handler exception must not kill the dispatcher.

    Inject a sequence where one event's handler attribute-touches a
    broken context and raises; the loop must keep dispatching.
    """
    from agent_transport.sip.livekit.audio_stream_server import AudioStreamServer

    srv = AudioStreamServer.__new__(AudioStreamServer)
    srv._ep = FakeEndpoint()
    srv._session_contexts = {}
    srv._session_ended_events = {}
    srv._active_sessions = {}
    srv._background_tasks = set()

    class BrokenCtx:
        def __getattr__(self, name):
            raise RuntimeError("simulated ctx failure")

    # We dispatch participant_connected for "session-A" (creates a
    # background task that immediately fails because _start_session
    # isn't wired — that failure is observed via background_tasks).
    # Then participant_disconnected for "session-dead" whose context's
    # event lookup raises — exception must be caught. Then
    # participant_connected for "session-B" — must still be processed.
    srv._session_contexts["session-dead"] = BrokenCtx()
    # Replace _start_session with a sentinel that records the call but
    # doesn't actually start a session. We assert against this list to
    # confirm the loop kept dispatching past the broken-ctx event.
    started = []

    async def _stub_start(session_id, *a, **k):
        srv._active_sessions[session_id] = asyncio.current_task()
        started.append(session_id)
    srv._start_session = _stub_start

    loop_task = asyncio.create_task(srv._lifecycle_loop())
    # Give the subscribe a tick to register.
    await asyncio.sleep(0.01)

    isolated_global.put(_ev_participant_connected("session-A"))
    isolated_global.put(_ev_participant_disconnected("session-dead"))
    isolated_global.put(_ev_participant_connected("session-B"))

    # Wait until both participant_connected events get processed.
    for _ in range(50):
        await asyncio.sleep(0.02)
        if "session-A" in started and "session-B" in started:
            break

    loop_task.cancel()
    try:
        await asyncio.wait_for(loop_task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert "session-A" in started
    assert "session-B" in started, (
        "lifecycle loop died after the broken participant_disconnected — "
        "session-B was never processed"
    )


@pytest.mark.asyncio
async def test_sip_server_lifecycle_loop_survives_handler_exception(isolated_global):
    """Same regression test for the SIP server. We trigger a broken
    handler via ``participant_disconnected`` whose registered ctx
    raises on attribute access, then verify subsequent events still get
    dispatched.
    """
    from agent_transport.sip.livekit.server import AgentServer

    srv = AgentServer.__new__(AgentServer)
    srv._ep = FakeEndpoint()
    srv._call_contexts = {}
    srv._call_ended_events = {}
    srv._active_calls = {}
    srv._background_tasks = set()
    srv._outbound_session_ids = set()
    srv._server_listeners = {}

    class BrokenCtx:
        def __getattr__(self, name):
            raise RuntimeError("simulated ctx failure")

    srv._call_contexts["call-dead"] = BrokenCtx()
    started = []

    async def _stub_start(session_id, *a, **k):
        srv._active_calls[session_id] = asyncio.current_task()
        started.append(session_id)
    srv._start_call = _stub_start

    loop_task = asyncio.create_task(srv._lifecycle_loop())
    await asyncio.sleep(0.01)

    isolated_global.put(_ev_participant_disconnected("call-dead"))
    isolated_global.put(_ev_participant_connected("call-X"))
    # And one more so we can verify the loop is still alive
    isolated_global.put(_ev_participant_connected("call-Y"))

    for _ in range(50):
        await asyncio.sleep(0.02)
        if "call-X" in started and "call-Y" in started:
            break

    loop_task.cancel()
    try:
        await asyncio.wait_for(loop_task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert "call-X" in started
    assert "call-Y" in started
