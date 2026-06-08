"""Tests for ``_run_cleanup`` on the LiveKit ``AgentServer`` /
``AudioStreamServer``.

``_run_cleanup`` is the cleanup body that the SIGINT/SIGTERM handler runs
before ``os._exit(0)``. The signal-handler path itself isn't unit-testable
(can't kill our own test process), so we extracted the cleanup body into a
plain async method and test it directly.

What we lock down:

  * every active session is hung up (one ``ep.hangup`` per id), in iteration order
  * if one cleanup step raises, later steps still run — partial cleanup is
    better than no cleanup
  * a stuck async step is bounded by the 2-second per-step ``wait_for``
  * absent optional resources (no inference executor, no endpoint) are tolerated
"""

import asyncio
import time
from unittest.mock import MagicMock, AsyncMock

import pytest


class _FakeEndpoint:
    def __init__(self):
        self.hangups: list[str] = []
        self.shutdown_calls = 0
        self.hangup_raises_for: set[str] = set()

    def hangup(self, session_id: str) -> None:
        if session_id in self.hangup_raises_for:
            raise RuntimeError(f"simulated hangup failure for {session_id}")
        self.hangups.append(session_id)

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class _FakeLoadMonitor:
    def __init__(self):
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class _FakeRunner:
    def __init__(self, *, raises: bool = False, hangs: bool = False):
        self.cleanup_calls = 0
        self._raises = raises
        self._hangs = hangs

    async def cleanup(self) -> None:
        self.cleanup_calls += 1
        if self._raises:
            raise RuntimeError("simulated runner.cleanup failure")
        if self._hangs:
            await asyncio.sleep(60)


def _server(cls):
    """Build a bare server instance with the attributes ``_run_cleanup`` reads."""
    srv = cls.__new__(cls)
    srv._ep = _FakeEndpoint()
    srv._load_monitor = _FakeLoadMonitor()
    srv._inference_executor = None
    if cls.__name__ == "AgentServer":
        srv._active_calls = {}
    else:
        srv._active_sessions = {}
    return srv


def _active_map(srv):
    """Return whichever dict the server uses for active session/call ids."""
    if hasattr(srv, "_active_calls"):
        return srv._active_calls
    return srv._active_sessions


# Parametrize every test over both server classes — both implementations
# share the same cleanup contract, so divergence is exactly the regression
# we want to catch.
@pytest.fixture(params=["sip", "audio_stream"])
def server_cls(request):
    if request.param == "sip":
        from agent_transport.sip.livekit.server import AgentServer
        return AgentServer
    else:
        from agent_transport.sip.livekit.audio_stream_server import AudioStreamServer
        return AudioStreamServer


@pytest.mark.asyncio
async def test_run_cleanup_hangs_up_all_active_sessions(server_cls):
    """The first thing cleanup must do is hangup every active session.

    If it skipped this and went straight to ``ep.shutdown()``, in-flight RTP
    streams could outlive the process. Order is deterministic (insertion
    order on a Python dict).
    """
    srv = _server(server_cls)
    active = _active_map(srv)
    active["sess-1"] = object()
    active["sess-2"] = object()
    active["sess-3"] = object()

    runner = _FakeRunner()
    event_task = asyncio.create_task(asyncio.sleep(60))
    loop = asyncio.get_running_loop()

    await srv._run_cleanup(runner, event_task, loop)
    # `event_task.cancel()` only requests cancellation — drain it so we can
    # assert it actually reached the cancelled state.
    try:
        await event_task
    except asyncio.CancelledError:
        pass

    assert srv._ep.hangups == ["sess-1", "sess-2", "sess-3"]
    assert event_task.cancelled() or event_task.done()
    assert runner.cleanup_calls == 1
    assert srv._load_monitor.stop_calls == 1
    assert srv._ep.shutdown_calls == 1


@pytest.mark.asyncio
async def test_run_cleanup_continues_when_a_step_raises(server_cls):
    """A failing step must not abort the rest of cleanup.

    The whole point of force-exit shutdown is that we get out even when one
    component is misbehaving. If runner.cleanup() raises, ep.shutdown still
    has to be called or the Rust endpoint pins libuv forever.
    """
    srv = _server(server_cls)
    active = _active_map(srv)
    active["sess-A"] = object()
    active["sess-B"] = object()
    srv._ep.hangup_raises_for = {"sess-A"}  # first hangup throws
    srv._inference_executor = MagicMock()
    srv._inference_executor.aclose = AsyncMock(
        side_effect=RuntimeError("simulated executor failure"),
    )

    runner = _FakeRunner(raises=True)
    event_task = asyncio.create_task(asyncio.sleep(60))
    loop = asyncio.get_running_loop()

    # Must not raise.
    await srv._run_cleanup(runner, event_task, loop)

    # First hangup threw, second still ran.
    assert srv._ep.hangups == ["sess-B"]
    # Stop and shutdown still ran after the failing steps.
    assert srv._load_monitor.stop_calls == 1
    assert srv._ep.shutdown_calls == 1


@pytest.mark.asyncio
async def test_run_cleanup_tolerates_missing_optional_resources(server_cls):
    """No inference executor, no endpoint, no active sessions ⇒ no-op-ish.

    Production starts the server before connecting transport — a SIGINT
    during that window hits cleanup with half the fields ``None``.
    """
    srv = _server(server_cls)
    srv._ep = None
    srv._inference_executor = None

    runner = _FakeRunner()
    event_task = asyncio.create_task(asyncio.sleep(60))
    loop = asyncio.get_running_loop()

    await srv._run_cleanup(runner, event_task, loop)

    assert runner.cleanup_calls == 1
    assert srv._load_monitor.stop_calls == 1


@pytest.mark.asyncio
async def test_run_cleanup_bounded_by_per_step_timeout_on_hang(server_cls):
    """If runner.cleanup hangs, the whole cleanup must still complete within
    a small multiple of the 2-second per-step timeout.

    Without ``asyncio.wait_for`` this test would never finish (the runner
    sleeps for 60s). The bound is per-step, so total time ≤ ~4s end-to-end
    even with one hung step.
    """
    srv = _server(server_cls)
    runner = _FakeRunner(hangs=True)
    event_task = asyncio.create_task(asyncio.sleep(60))
    loop = asyncio.get_running_loop()

    t0 = time.monotonic()
    await srv._run_cleanup(runner, event_task, loop)
    elapsed = time.monotonic() - t0

    assert elapsed < 4.0, f"cleanup ran {elapsed:.2f}s — timeout not honored"
    # The endpoint should still have been shut down after the timeout fired.
    assert srv._ep.shutdown_calls == 1
