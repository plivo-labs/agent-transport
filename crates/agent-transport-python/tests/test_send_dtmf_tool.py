"""Regression test for LiveKit's `send_dtmf_events` beta tool.

Docs: https://docs.livekit.io/reference/python/livekit/agents/beta/tools/send_dtmf.html

The tool calls `job_ctx.room.local_participant.publish_dtmf(code=..., digit=...)`
for each event in turn. Our `_TransportLocalParticipant.publish_dtmf` maps
this to `ep.send_dtmf(session_id, digit)` on the Rust endpoint.

Verified properties:
1. `publish_dtmf` routes through to `ep.send_dtmf` with the right session id
   and digit string.
2. `publish_dtmf` runs on the default executor so the async loop is not
   blocked while Rust paces DTMF RTP packets (~280 ms per digit).
3. The `send_dtmf_events` tool can drive our `local_participant.publish_dtmf`
   end-to-end for a multi-digit PIN without losing any digit.
"""

import asyncio
import inspect
import time
import types

import pytest

from agent_transport.sip.livekit._room_facade import (
    TransportRoom, _TransportLocalParticipant,
)


class _SlowFakeEndpoint:
    """send_dtmf sleeps 50 ms per digit (simulates the RTP pacing in Rust)."""

    def __init__(self):
        self.input_sample_rate = 8000
        self.output_sample_rate = 8000
        self.calls = []  # list of (session_id, digit, thread_id)

    def send_dtmf(self, session_id, digit):
        import threading
        time.sleep(0.05)
        self.calls.append((session_id, digit, threading.get_ident()))


def _make_room(endpoint=None):
    ep = endpoint or _SlowFakeEndpoint()
    return TransportRoom(
        endpoint=ep,
        session_id="call-dtmf",
        agent_name="agent",
        caller_identity="sip:caller@x",
    )


# ─── publish_dtmf direct ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_publish_dtmf_routes_to_endpoint():
    """Calling publish_dtmf invokes ep.send_dtmf with session id + digit."""
    room = _make_room()
    ep = room._ep

    await room.local_participant.publish_dtmf(code=1, digit="1")
    await room.local_participant.publish_dtmf(code=2, digit="2")
    await room.local_participant.publish_dtmf(code=11, digit="#")

    digits = [(sid, d) for sid, d, _tid in ep.calls]
    assert digits == [
        ("call-dtmf", "1"),
        ("call-dtmf", "2"),
        ("call-dtmf", "#"),
    ]


@pytest.mark.asyncio
async def test_publish_dtmf_does_not_block_event_loop():
    """Core guarantee: publish_dtmf hops to the executor so the asyncio loop
    can service other tasks (e.g., the audio recv loop, STT, TTS writes)
    while Rust paces DTMF RTP packets. If publish_dtmf were to run on the
    loop thread, the concurrent heartbeat below would be delayed by the
    total DTMF duration.
    """
    room = _make_room()
    heartbeats = 0

    async def heartbeat():
        nonlocal heartbeats
        while True:
            heartbeats += 1
            await asyncio.sleep(0.01)

    hb = asyncio.create_task(heartbeat())
    start = time.monotonic()

    # 4-digit PIN @ 50ms per digit sleep = 200ms of executor work
    for d in "1234":
        await room.local_participant.publish_dtmf(code=int(d), digit=d)

    elapsed = time.monotonic() - start
    hb.cancel()
    try:
        await hb
    except asyncio.CancelledError:
        pass

    # If publish_dtmf blocks the loop, heartbeats == 0 or very few.
    # On the executor, we expect ~200ms / 10ms = ~20 heartbeats
    # (allow generous slack: >=10).
    assert heartbeats >= 10, (
        f"publish_dtmf appears to be blocking the event loop "
        f"(only {heartbeats} heartbeats in {elapsed:.2f}s across 4 DTMF digits)"
    )


@pytest.mark.asyncio
async def test_publish_dtmf_runs_on_executor_thread():
    """publish_dtmf must hop to a worker thread for the Rust FFI call,
    not run on the loop thread itself.
    """
    import threading
    room = _make_room()
    loop_thread_id = threading.get_ident()

    await room.local_participant.publish_dtmf(code=1, digit="1")

    assert len(room._ep.calls) == 1
    _sid, _digit, tid = room._ep.calls[0]
    assert tid != loop_thread_id, (
        "publish_dtmf ran on the loop thread — should be on executor"
    )


# ─── end-to-end with the real send_dtmf_events tool ─────────────────────────

@pytest.mark.asyncio
async def test_send_dtmf_events_tool_drives_publish_dtmf():
    """The upstream LiveKit tool send_dtmf_events iterates events and calls
    local_participant.publish_dtmf for each. Verify that plumbing works
    against our stub room.
    """
    try:
        from livekit.agents.beta.tools.send_dtmf import send_dtmf_events
        from livekit.agents.beta.workflows.utils import DtmfEvent
    except ImportError:
        pytest.skip("livekit.agents.beta.tools.send_dtmf not available")

    room = _make_room()
    ep = room._ep

    class _FakeJobCtx:
        def __init__(self, r):
            self.room = r

    # On livekit-agents <= 1.6.0 send_dtmf.py resolves the room via
    # `get_job_context()`, which reads from the `_JobContextVar` ContextVar.
    # Patching `get_job_context` is not enough because the tool imports it
    # by name (`from ...job import get_job_context`) so the imported
    # reference is frozen. Set the ContextVar directly so the real
    # `get_job_context` returns our stub.
    from livekit.agents.job import _JobContextVar

    # Tool is wrapped in @function_tool; raw callable is at `._fnc`,
    # `.__wrapped__`, `.info.fnc`, or the object itself.
    raw_fn = (
        getattr(send_dtmf_events, "_fnc", None)
        or getattr(send_dtmf_events, "__wrapped__", None)
        or getattr(getattr(send_dtmf_events, "info", None), "fnc", None)
        or send_dtmf_events
    )

    token = _JobContextVar.set(_FakeJobCtx(room))
    try:
        events = [DtmfEvent.ONE, DtmfEvent.TWO, DtmfEvent.THREE, DtmfEvent.POUND]
        if "ctx" in inspect.signature(raw_fn).parameters:
            # livekit-agents > 1.6.0: the tool takes a RunContext and reads
            # the room from `ctx.session.room_io.room` (get_job_context()
            # remains as fallback only).
            fake_ctx = types.SimpleNamespace(
                session=types.SimpleNamespace(
                    room_io=types.SimpleNamespace(room=room)
                )
            )
            result = await raw_fn(fake_ctx, events=events)
        else:
            # livekit-agents <= 1.6.0: the tool reads get_job_context().room,
            # served by the ContextVar set above.
            result = await raw_fn(events=events)
    finally:
        _JobContextVar.reset(token)

    # All four digits should have been dispatched in order
    sent = [(sid, d) for sid, d, _tid in ep.calls]
    assert sent == [
        ("call-dtmf", "1"),
        ("call-dtmf", "2"),
        ("call-dtmf", "3"),
        ("call-dtmf", "#"),
    ]
    assert "Successfully sent DTMF events" in result or "sent" in result.lower()
