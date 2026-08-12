"""Teardown-close coverage for the transport audio I/O leak fix.

Upstream, ``RoomIO.aclose()`` is the only caller of the audio output's
``aclose()`` — and both servers here bypass RoomIO. ``AgentSession``'s
``_aclose_impl`` merely DETACHES (``output.audio = None``), so before this
fix every call leaked the output's pending ``_forwarding_task``, surfacing
in prod as one "Task was destroyed but it is pending!" per call, minutes to
hours after the call ended (whenever GC reaped the cycle).

The fix has two halves, each locked here:

1. **Refs**: the JobContext ``session`` setter keeps ``_audio_input`` /
   ``_audio_output`` refs so teardown can reach the I/O after the session
   has detached it (behavioral tests, real ``TransportAudioOutput``).
2. **Wiring**: ``_run_session``'s end path in BOTH servers closes the I/O
   via those refs after ``session.aclose()`` has drained (source pins —
   driving the full ``_run_session`` needs the Rust endpoint; the pin locks
   the call site the way ``test_force_shutdown_wiring`` locks #83's).
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest


class _FakeEp:
    input_sample_rate = 8000
    output_sample_rate = 8000

    def recv_audio_bytes_blocking(self, sid, ms):
        return None

    def hangup(self, sid):
        pass


class _FakeSession:
    """Accepts the setter's wiring writes and close-handler registration."""

    def __init__(self):
        self.input = SimpleNamespace(audio=None)
        self.output = SimpleNamespace(audio=None)

    def on(self, event_name, callback=None):
        if callback is None:
            return lambda fn: fn
        return callback


def _make_ctx(server_module: str):
    if server_module == "audio_stream":
        from agent_transport.sip.livekit.audio_stream_server import JobContext

        return JobContext(
            session_id="sid-test",
            plivo_call_uuid="call-test",
            stream_id="stream-test",
            direction="inbound",
            extra_headers={},
            endpoint=_FakeEp(),
        )
    from agent_transport.sip.livekit.server import JobContext

    return JobContext(
        session_id="sid-test",
        remote_uri="sip:test@example.com",
        direction="inbound",
        endpoint=_FakeEp(),
    )


@pytest.mark.parametrize("server_module", ["audio_stream", "sip"])
def test_session_setter_stores_audio_io_refs(server_module):
    """The refs must be the same objects wired onto the session."""
    ctx = _make_ctx(server_module)
    session = _FakeSession()
    ctx.session = session

    assert ctx._audio_input is session.input.audio is not None
    assert ctx._audio_output is session.output.audio is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("server_module", ["audio_stream", "sip"])
async def test_output_closeable_after_session_detach(server_module):
    """The exact prod sequence: forwarding task started, session detaches
    the output (as _aclose_impl does), then teardown closes via the ctx ref
    — the forwarding task must end, not linger for GC."""
    ctx = _make_ctx(server_module)
    session = _FakeSession()
    ctx.session = session

    out = ctx._audio_output
    # Start the forwarding task the way production does (capture_frame's
    # auto-start guard); resolve the subscription first.
    await out._publish_track()
    out._forwarding_task = asyncio.ensure_future(out._forward_audio())
    await asyncio.sleep(0.01)
    assert not out._forwarding_task.done()

    # AgentSession._aclose_impl detaches without closing:
    session.output.audio = None

    await out.aclose()
    assert out._forwarding_task.done(), (
        "forwarding task still pending after aclose — the per-call leak"
    )


@pytest.mark.parametrize(
    "module_path",
    [
        "agent_transport.sip.livekit.audio_stream_server",
        "agent_transport.sip.livekit.server",
    ],
)
def test_run_session_end_path_closes_audio_io(module_path):
    """Wiring pin: the end path must close BOTH refs. If a refactor drops
    this loop, the per-call _forwarding_task leak returns with every other
    test green."""
    import importlib

    mod = importlib.import_module(module_path)
    src = inspect.getsource(mod)
    needle = "for _io in (ctx._audio_output, ctx._audio_input):"
    assert needle in src, (
        f"{module_path}: _run_session's end path no longer closes the "
        f"transport audio I/O via the JobContext refs ({needle!r} missing)."
    )
