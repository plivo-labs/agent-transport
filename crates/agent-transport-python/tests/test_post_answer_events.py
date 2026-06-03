"""Regression tests for the post-answer event refactor.

Covers (post-Tier-A architecture):

1. ``server.on("ringing", ...)`` fires when the lifecycle loop dispatches
   a ``transport_event.call_ringing`` FfiEvent.
2. The SIP server creates the agent session on
   ``room_event.participant_connected`` (the LiveKit-shape translation
   of Rust's ``call_answered``).
3. The audio_stream server creates the agent session on the same event.
4. Pipecat SIP ``SipServer.call()`` still starts the session directly
   after ``ep.call()`` returns — unchanged from the pre-Tier-A path.
5. Outbound ``participant_connected`` events are skipped when the session
   id is reserved in ``_outbound_session_ids`` — the outbound path owns
   session creation and must not be double-dispatched by the loop.
6. The event sink drops unknown Rust event types so a forward-incompatible
   Rust core can't crash the adapter.
"""

import asyncio
import pytest

from agent_transport import _ffi_queue as _ffi
from agent_transport._event import (
    CallRinging,
    FfiEvent,
    ParticipantConnected,
    ParticipantDisconnected,
    ParticipantInfo,
    RoomEvent,
    TransportEvent,
)
from agent_transport._event_sink import _build_ffi_events


# ─── Shared fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def isolated_global():
    """Swap GLOBAL for a fresh FfiQueue — see test_livekit_server_event_loop."""
    saved = _ffi.get_global()
    fresh = _ffi.FfiQueue()
    _ffi.set_global(fresh)
    from agent_transport.sip.livekit import audio_stream_server, server
    audio_stream_server.GLOBAL = fresh
    server.GLOBAL = fresh
    yield fresh
    _ffi.set_global(saved)
    audio_stream_server.GLOBAL = saved
    server.GLOBAL = saved


class _FakeRustSession:
    """Stand-in for Rust ``CallSession`` (attr access only)."""
    def __init__(self, session_id, remote_uri="sip:caller@x", direction="Inbound"):
        self.session_id = session_id
        self.remote_uri = remote_uri
        self.local_uri = "stream-id-1"
        self.extra_headers = {}
        self.call_uuid = f"uuid-{session_id}"
        self.direction = direction


class FakeEndpoint:
    def __init__(self):
        self.clear_buffer_calls = 0
        self.call_calls: list[tuple[str, ...]] = []

    def clear_buffer(self, session_id):
        self.clear_buffer_calls += 1

    def hangup(self, session_id):
        pass

    # Pipecat outbound calls this.
    def call(self, dest_uri, from_uri=None, headers=None):
        self.call_calls.append((dest_uri, from_uri, headers))
        return "outbound-session-1"


def _event_for_ringing(session_id: str, remote_uri: str) -> FfiEvent:
    return FfiEvent(
        transport_event=TransportEvent(
            call_ringing=CallRinging(
                session_id=session_id,
                remote_uri=remote_uri,
                call_uuid=f"uuid-{session_id}",
            )
        )
    )


def _event_for_participant_connected(session_id: str, remote_uri: str) -> FfiEvent:
    return FfiEvent(
        room_event=RoomEvent(
            room_handle=session_id,
            participant_connected=ParticipantConnected(
                info=ParticipantInfo(
                    identity=remote_uri,
                    session_id=session_id,
                    stream_id="stream-id-1",
                    extra_headers={},
                ),
            ),
        )
    )


# ─── LiveKit AgentServer (SIP): ringing + participant_connected ──────────


@pytest.mark.asyncio
async def test_livekit_sip_server_fires_ringing_hook(isolated_global):
    from agent_transport.sip.livekit.server import AgentServer

    srv = AgentServer.__new__(AgentServer)
    srv._ep = FakeEndpoint()
    srv._call_contexts = {}
    srv._call_ended_events = {}
    srv._active_calls = {}
    srv._background_tasks = set()
    srv._outbound_session_ids = set()
    srv._server_listeners = {}

    ringing_observed = []

    @srv.on("ringing")
    def _on_ringing(session):
        ringing_observed.append((session.session_id, session.remote_uri))

    loop_task = asyncio.create_task(srv._lifecycle_loop())
    await asyncio.sleep(0.01)
    isolated_global.put(_event_for_ringing("sip-call-1", "sip:alice@x"))

    for _ in range(50):
        await asyncio.sleep(0.02)
        if ringing_observed:
            break

    loop_task.cancel()
    try:
        await asyncio.wait_for(loop_task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert ringing_observed == [("sip-call-1", "sip:alice@x")]


@pytest.mark.asyncio
async def test_livekit_sip_server_starts_session_on_participant_connected(isolated_global):
    from agent_transport.sip.livekit.server import AgentServer

    srv = AgentServer.__new__(AgentServer)
    srv._ep = FakeEndpoint()
    srv._call_contexts = {}
    srv._call_ended_events = {}
    srv._active_calls = {}
    srv._background_tasks = set()
    srv._outbound_session_ids = set()
    srv._server_listeners = {}

    start_call_invocations = []

    async def fake_start_call(sid, uri, direction):
        start_call_invocations.append((sid, uri, direction))

    srv._start_call = fake_start_call

    loop_task = asyncio.create_task(srv._lifecycle_loop())
    await asyncio.sleep(0.01)
    isolated_global.put(_event_for_participant_connected("sip-call-42", "sip:bob@x"))

    for _ in range(50):
        await asyncio.sleep(0.02)
        if start_call_invocations:
            break

    loop_task.cancel()
    try:
        await asyncio.wait_for(loop_task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert start_call_invocations == [("sip-call-42", "sip:bob@x", "inbound")]


@pytest.mark.asyncio
async def test_livekit_sip_server_skips_outbound_participant_connected(isolated_global):
    """Outbound sessions are reserved in `_outbound_session_ids`. The
    lifecycle loop must NOT create a second agent session for them.
    """
    from agent_transport.sip.livekit.server import AgentServer

    srv = AgentServer.__new__(AgentServer)
    srv._ep = FakeEndpoint()
    srv._call_contexts = {}
    srv._call_ended_events = {}
    srv._active_calls = {}
    srv._background_tasks = set()
    srv._outbound_session_ids = {"outbound-1"}
    srv._server_listeners = {}

    start_call_invocations = []

    async def fake_start_call(sid, uri, direction):
        start_call_invocations.append((sid, uri, direction))

    srv._start_call = fake_start_call

    loop_task = asyncio.create_task(srv._lifecycle_loop())
    await asyncio.sleep(0.01)
    isolated_global.put(_event_for_participant_connected("outbound-1", "sip:peer@x"))

    for _ in range(15):
        await asyncio.sleep(0.02)

    loop_task.cancel()
    try:
        await asyncio.wait_for(loop_task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert start_call_invocations == []
    assert "outbound-1" not in srv._outbound_session_ids


# ─── LiveKit AudioStreamServer: single-phase session start ───────────────


@pytest.mark.asyncio
async def test_livekit_audio_stream_server_starts_on_participant_connected(isolated_global):
    """Audio_stream — session starts immediately on the LiveKit-shape
    participant_connected (translated from Rust's ``call_answered``)."""
    from agent_transport.sip.livekit.audio_stream_server import AudioStreamServer

    srv = AudioStreamServer.__new__(AudioStreamServer)
    srv._ep = FakeEndpoint()
    srv._active_sessions = {}
    srv._session_ended_events = {}
    srv._session_contexts = {}
    srv._background_tasks = set()
    srv._server_listeners = {}

    started = []

    async def fake_start(sid, call_uuid, stream_id, extra):
        started.append((sid, call_uuid, stream_id, dict(extra)))

    srv._start_session = fake_start

    loop_task = asyncio.create_task(srv._lifecycle_loop())
    await asyncio.sleep(0.01)
    isolated_global.put(_event_for_participant_connected("ws-1", "call-uuid-xyz"))

    for _ in range(50):
        await asyncio.sleep(0.02)
        if started:
            break

    loop_task.cancel()
    try:
        await asyncio.wait_for(loop_task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert len(started) == 1
    assert started[0][0] == "ws-1"
    assert started[0][1] == "call-uuid-xyz"
    assert started[0][2] == "stream-id-1"


# ─── Pipecat SIP: SipServerTransport.call() starts session directly ─────


@pytest.mark.asyncio
async def test_pipecat_sip_call_starts_session_directly():
    """SipServerTransport.call() populates outbound_session_ids and
    invokes _start_session() directly. Unchanged from pre-Tier-A.
    """
    from agent_transport.sip.pipecat.transports.sip import SipServerTransport

    srv = SipServerTransport.__new__(SipServerTransport)
    srv._ep = FakeEndpoint()
    srv._active_sessions = {}
    srv._session_start_times = {}
    srv._session_event_queues = {}
    srv._background_tasks = set()
    srv._outbound_session_ids = set()
    srv._server_listeners = {}
    srv._handler_fnc = None
    srv._transport_params = None

    start_invocations = []

    def fake_start(session_id, session_data):
        start_invocations.append((session_id, dict(session_data)))

    srv._start_session = fake_start

    result = await srv.call(
        "sip:+1234567890@phone.plivo.com",
        from_uri="sip:+0987654321@phone.plivo.com",
    )

    assert result == "outbound-session-1"
    assert len(start_invocations) == 1
    assert start_invocations[0][0] == "outbound-session-1"
    assert start_invocations[0][1]["direction"] == "Outbound"
    assert "outbound-session-1" in srv._outbound_session_ids
    assert srv._ep.call_calls == [(
        "sip:+1234567890@phone.plivo.com",
        "sip:+0987654321@phone.plivo.com",
        None,
    )]


# ─── Sink ignores unknown Rust event types ───────────────────────────────


def test_sink_drops_unknown_event_types():
    """If a future Rust core accidentally emits an unknown ``type`` (or
    if a pre-refactor legacy name like ``incoming_call`` slips back in),
    the sink must return zero FfiEvents — the lifecycle loop never sees
    the stray dict at all. Guardrail against silent regressions.
    """
    for d in (
        {"type": "incoming_call", "session": _FakeRustSession("x")},
        {"type": "call_media_active", "session_id": "x"},
        {"type": "nonexistent_future_event"},
    ):
        events = _build_ffi_events(d)
        assert events == ()
