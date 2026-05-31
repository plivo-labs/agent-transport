"""Regression test for Pipecat WebsocketServerTransport._event_loop.

The server's dispatcher subscribes to ``GLOBAL_DICT`` (the dict-shaped
sibling of the LiveKit FfiQueue, fed by the shared event sink) and
routes events to per-session asyncio queues. A malformed event must
not crash the dispatcher — a try/except around the per-event handler
swallows the exception and the loop continues with the next event.

This test feeds events directly into GLOBAL_DICT, mimicking what the
Rust event sink does after ``set_event_sink`` is installed.
"""

import asyncio
import pytest

from agent_transport._ffi_queue import GLOBAL_DICT
from agent_transport.audio_stream.pipecat.transports.websocket import (
    WebsocketServerTransport,
)


class _FakeSession:
    def __init__(self, sid, call_uuid="cu", local_uri="stream"):
        self.session_id = sid
        self.call_uuid = call_uuid
        self.remote_uri = "sip:x@x"
        self.local_uri = local_uri
        self.direction = "inbound"
        self.extra_headers = {}


@pytest.mark.asyncio
async def test_ws_event_loop_survives_malformed_event():
    srv = WebsocketServerTransport.__new__(WebsocketServerTransport)
    srv._ep = None  # event_loop no longer touches the endpoint
    srv._session_event_queues = {}
    srv._active_sessions = {}
    srv._session_start_times = {}

    # Track which event types reached the routing branches (handled events).
    routed: list = []
    original_get = srv._session_event_queues.get
    # We need to observe what the loop does without registering real
    # sessions — _start_session would build a real transport. So feed
    # only events that route into _session_event_queues OR hit the
    # malformed branch.
    # Track call_terminated routing via a fake queue we register.
    q = asyncio.Queue()
    srv._session_event_queues["session-good"] = q

    # Start the loop first so the subscription is in place before we
    # put events into GLOBAL_DICT. ``FfiQueue.put`` dispatches via
    # ``loop.call_soon_threadsafe`` to the currently-subscribed list —
    # items put before subscribe() are simply not delivered to that
    # subscriber.
    loop_task = asyncio.create_task(srv._event_loop())
    # Give the task one tick to call subscribe() inside _event_loop.
    await asyncio.sleep(0.05)

    # Malformed: missing "session" key — call_terminated branch reads
    # ``event["session"]`` and raises KeyError. Loop must swallow.
    GLOBAL_DICT.put({"type": "call_terminated"})
    # Well-formed call_terminated routed to per-session queue.
    GLOBAL_DICT.put({
        "type": "call_terminated",
        "session": _FakeSession("session-good"),
    })
    # Well-formed dtmf_received for unknown session — routed branch
    # tolerates the unknown id silently.
    GLOBAL_DICT.put({
        "type": "dtmf_received",
        "session_id": "unknown",
        "digit": "5",
    })

    # Wait for the second well-formed call_terminated to land in the
    # per-session queue — that proves the loop survived the malformed
    # one AND processed the well-formed one.
    try:
        routed_event = await asyncio.wait_for(q.get(), timeout=2.0)
        assert routed_event.get("type") == "call_terminated"
        assert routed_event["session"].session_id == "session-good"
    finally:
        loop_task.cancel()
        try:
            await asyncio.wait_for(loop_task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
