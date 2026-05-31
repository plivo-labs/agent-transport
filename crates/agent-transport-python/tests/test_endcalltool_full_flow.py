"""Regression test for the LiveKit EndCallTool flow on SIP transport.

EndCallTool calls ``job_ctx.shutdown(reason=...)`` and expects:
1. Any registered shutdown callbacks (both 0-arg and 1-arg variants) fire
   with the correct reason string.
2. The underlying SIP/audio_stream call gets dropped via ep.hangup().
3. The flow tolerates callback exceptions (one bad callback doesn't
   block the others or the hangup).
"""

import asyncio
import pytest

from agent_transport.sip.livekit._room_facade import TransportRoom, _StubJobContext


class FakeEndpoint:
    def __init__(self):
        self.input_sample_rate = 8000
        self.hangup_calls = []
        self.stop_recording_calls = []

    def hangup(self, session_id):
        self.hangup_calls.append(session_id)

    def stop_recording(self, session_id):
        self.stop_recording_calls.append(session_id)


def _make_ctx():
    ep = FakeEndpoint()
    room = TransportRoom(
        endpoint=ep, session_id="call-42",
        agent_name="agent", caller_identity="sip:caller@x",
    )
    return _StubJobContext(room=room, agent_name="agent"), room, ep


@pytest.mark.asyncio
async def test_shutdown_fires_one_arg_callback_with_reason():
    ctx, _, ep = _make_ctx()
    received = []

    async def on_shutdown(reason: str) -> None:
        received.append(reason)

    ctx.add_shutdown_callback(on_shutdown)
    ctx.shutdown(reason="user-requested")
    # shutdown() schedules the async callback; yield to let it run.
    await asyncio.sleep(0.05)

    assert received == ["user-requested"]
    assert ep.hangup_calls == ["call-42"]


@pytest.mark.asyncio
async def test_shutdown_fires_zero_arg_callback():
    """0-arg callbacks must be wrapped and still run."""
    ctx, _, ep = _make_ctx()
    fired = asyncio.Event()

    async def on_shutdown() -> None:
        fired.set()

    ctx.add_shutdown_callback(on_shutdown)
    ctx.shutdown(reason="ignored-by-zero-arg-cb")
    await asyncio.wait_for(fired.wait(), timeout=1.0)

    assert ep.hangup_calls == ["call-42"]


@pytest.mark.asyncio
async def test_shutdown_fires_sync_zero_arg_callback():
    """Sync 0-arg callbacks must run without being awaited."""
    ctx, _, ep = _make_ctx()
    fired = []

    def on_shutdown() -> None:
        fired.append("called")

    ctx.add_shutdown_callback(on_shutdown)
    ctx.shutdown(reason="ignored-by-zero-arg-cb")

    # The sync callback runs inline, but on 0.2.0 the hangup is dispatched
    # OFF the loop (schedule_hangup -> control_executor thread pool), so one
    # loop tick does not guarantee it has run. Poll until observed.
    assert fired == ["called"]
    for _ in range(200):
        if ep.hangup_calls:
            break
        await asyncio.sleep(0.005)
    assert ep.hangup_calls == ["call-42"]


@pytest.mark.asyncio
async def test_shutdown_tolerates_bad_callback():
    """A raising callback must not prevent other callbacks or the hangup."""
    ctx, _, ep = _make_ctx()
    results = []

    async def bad(reason: str) -> None:
        raise RuntimeError("boom")

    async def good(reason: str) -> None:
        results.append(reason)

    ctx.add_shutdown_callback(bad)
    ctx.add_shutdown_callback(good)
    ctx.shutdown(reason="cleanup")
    await asyncio.sleep(0.05)

    # Good callback ran even though the first raised.
    assert results == ["cleanup"]
    # Hangup still fired.
    assert ep.hangup_calls == ["call-42"]


@pytest.mark.asyncio
async def test_shutdown_callbacks_fire_once():
    """shutdown() and final cleanup must not dispatch callbacks twice."""
    ctx, _, ep = _make_ctx()
    received = []

    async def on_shutdown(reason: str) -> None:
        received.append(reason)

    ctx.add_shutdown_callback(on_shutdown)
    ctx.shutdown(reason="tool-requested")
    await asyncio.sleep(0.05)
    await ctx._run_shutdown_callbacks("call ended")

    assert received == ["tool-requested"]
    assert ep.hangup_calls == ["call-42"]
