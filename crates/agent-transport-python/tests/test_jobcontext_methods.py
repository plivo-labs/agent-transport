"""Regression tests for _StubJobContext methods added in the parity audit.

Covers the LiveKit JobContext methods that previously raised AttributeError
when SIP agent code used them:

- wait_for_participant: returns the single remote caller synchronously
- add_sip_participant: wraps ep.call() in run_in_executor, returns Future
- transfer_sip_participant: wraps ep.transfer() in run_in_executor
- add_participant_entrypoint: fires the entrypoint for the single remote
- simulation_context: returns None (livekit-agents >= 1.6 probes it on start)

All endpoint calls must go through run_in_executor to release the GIL
while Rust does the real work (compare against raw endpoint blocks the
event loop).
"""

import asyncio
import pytest

from agent_transport.sip.livekit._room_facade import TransportRoom, _StubJobContext


class FakeEndpoint:
    """Records every call/transfer/hangup so we can assert on them."""

    def __init__(self):
        self.input_sample_rate = 8000
        self.call_calls = []
        self.transfer_calls = []
        self.hangup_calls = []

    def call(self, call_to, from_uri=None, headers=None):
        # Simulate a blocking Rust FFI call — if this ran on the event loop
        # thread instead of an executor, the test would deadlock via a
        # combined sleep+await pattern.
        self.call_calls.append((call_to, from_uri, headers))
        return "out-session-1"

    def transfer(self, session_id, target_uri):
        self.transfer_calls.append((session_id, target_uri))

    def hangup(self, session_id):
        self.hangup_calls.append(session_id)

    def stop_recording(self, session_id):
        pass


def _make_ctx(ep=None):
    ep = ep or FakeEndpoint()
    room = TransportRoom(
        endpoint=ep, session_id="call-1",
        agent_name="agent", caller_identity="sip:caller@x",
    )
    ctx = _StubJobContext(room=room, agent_name="agent")
    return ctx, room, ep


def test_simulation_context_returns_none():
    """livekit-agents >= 1.6 calls this from AgentSession._text_only on every
    session.start(); a transport-backed context is never a simulation, and None
    selects the normal voice path. Absent, start() raised AttributeError.
    """
    ctx, _, _ = _make_ctx()
    assert ctx.simulation_context() is None


@pytest.mark.asyncio
async def test_wait_for_participant_returns_single_remote():
    ctx, room, _ = _make_ctx()
    remote = await ctx.wait_for_participant()
    assert remote is room._remote
    assert remote.identity == "sip:caller@x"


@pytest.mark.asyncio
async def test_wait_for_participant_with_matching_identity():
    ctx, room, _ = _make_ctx()
    remote = await ctx.wait_for_participant(identity="sip:caller@x")
    assert remote is room._remote


@pytest.mark.asyncio
async def test_wait_for_participant_mismatched_identity_still_returns():
    """Mismatched identity logs a warning but still returns the single remote —
    the SIP transport has exactly one remote so there's nothing to wait for.
    """
    ctx, room, _ = _make_ctx()
    remote = await ctx.wait_for_participant(identity="sip:different@x")
    assert remote is room._remote


@pytest.mark.asyncio
async def test_add_sip_participant_dials_via_endpoint():
    ctx, _, ep = _make_ctx()
    info = await ctx.add_sip_participant(
        call_to="sip:target@x.com",
        participant_identity="target",
        participant_name="Target",
    )
    assert ep.call_calls == [("sip:target@x.com", None, None)]
    assert info.participant_identity == "target"
    assert info.participant_name == "Target"
    assert info.sip_call_id == "out-session-1"


@pytest.mark.asyncio
async def test_transfer_sip_participant_refers_via_endpoint():
    ctx, room, ep = _make_ctx()
    await ctx.transfer_sip_participant(
        participant=room._remote,
        transfer_to="sip:human@hq.com",
    )
    assert ep.transfer_calls == [("call-1", "sip:human@hq.com")]


@pytest.mark.asyncio
async def test_add_participant_entrypoint_fires_once_for_remote():
    ctx, room, _ = _make_ctx()
    seen = []

    def entrypoint(context, participant):
        seen.append((context, participant))

    ctx.add_participant_entrypoint(entrypoint)
    assert seen == [(ctx, room._remote)]


@pytest.mark.asyncio
async def test_add_participant_entrypoint_accepts_coroutine_fn():
    ctx, room, _ = _make_ctx()
    done = asyncio.Event()

    async def entrypoint(context, participant):
        done.set()

    ctx.add_participant_entrypoint(entrypoint)
    # Entrypoint is scheduled as a Task; yield so it runs.
    await asyncio.wait_for(done.wait(), timeout=1.0)
