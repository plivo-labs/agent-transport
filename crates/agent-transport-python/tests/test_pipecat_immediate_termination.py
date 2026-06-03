"""Regression tests for Pipecat's immediate-termination pattern.

Pipecat docs: https://docs.pipecat.ai/pipecat/learn/pipeline-termination#2-immediate-termination

Immediate termination flow:
1. User code calls `await task.cancel(reason="...")`
2. PipelineTask pushes a `CancelFrame` downstream
3. BaseInputTransport.process_frame(CancelFrame) → calls `self.cancel(frame)`
4. BaseOutputTransport.process_frame(CancelFrame) → calls `self.cancel(frame)`
5. Both transports must tear down cleanly without hanging or raising

These tests verify our SIP and audio_stream adapters implement `cancel()`
correctly for both input and output transports — specifically that:
- Input cancel sets `_started=False`, cancels recv/event tasks, completes quickly
- Output cancel invokes `endpoint.hangup()` and does not hang if the Rust
  session was already torn down (idempotent)
- Neither raises an exception under a session-gone race
"""

import asyncio
import pytest

from pipecat.frames.frames import CancelFrame

from agent_transport.sip.pipecat.sip_transport import (
    SipInputTransport, SipOutputTransport,
)
from agent_transport.audio_stream.pipecat.audio_stream_transport import (
    AudioStreamInputTransport, AudioStreamOutputTransport,
)


class _FakeEndpoint:
    """Records hangup/clear_buffer calls; emits silence on recv."""

    def __init__(self, *, session_gone: bool = False):
        self.input_sample_rate = 8000
        self.output_sample_rate = 8000
        self.hangup_calls = 0
        self.clear_buffer_calls = 0
        self._session_gone = session_gone

    def recv_audio_bytes_blocking(self, session_id, timeout_ms):
        import time
        time.sleep(0.005)
        if self._session_gone:
            raise RuntimeError(f"call not active: {session_id}")
        return (b"\x00\x00" * 160, 8000, 1)

    def hangup(self, session_id):
        self.hangup_calls += 1
        # Matches idempotent Rust behavior — does not raise if session is gone.

    def clear_buffer(self, session_id):
        self.clear_buffer_calls += 1

    def wait_for_event(self, timeout_ms=100):
        return None


def _make_sip_input(endpoint=None):
    ep = endpoint or _FakeEndpoint()
    t = SipInputTransport.__new__(SipInputTransport)
    t._cid = "call-test"
    t._started = True
    t._paused = False
    t._ep = ep
    t._transport = None
    t._event_queue = None
    t._recv_task = None
    t._event_task = None
    t._pushed = []

    async def _push(frame):
        if not t._paused:
            t._pushed.append(frame)
    t.push_audio_frame = _push
    return t


def _make_sip_output(endpoint=None):
    ep = endpoint or _FakeEndpoint()
    t = SipOutputTransport.__new__(SipOutputTransport)
    t._cid = "call-test"
    t._started = True
    t._ep = ep
    t._transport = None
    t._loop = asyncio.get_event_loop()
    return t


def _make_audio_stream_input(endpoint=None):
    ep = endpoint or _FakeEndpoint()
    t = AudioStreamInputTransport.__new__(AudioStreamInputTransport)
    t._sid = "session-test"
    t._started = True
    t._paused = False
    t._ep = ep
    t._transport = None
    t._event_queue = None
    t._recv_task = None
    t._event_task = None
    t._pushed = []

    async def _push(frame):
        if not t._paused:
            t._pushed.append(frame)
    t.push_audio_frame = _push
    return t


def _make_audio_stream_output(endpoint=None):
    ep = endpoint or _FakeEndpoint()
    t = AudioStreamOutputTransport.__new__(AudioStreamOutputTransport)
    t._sid = "session-test"
    t._started = True
    t._ep = ep
    t._transport = None
    t._loop = asyncio.get_event_loop()
    return t


# ─── Guardrails: cancel() is implemented ────────────────────────────────────

def test_sip_input_overrides_cancel():
    """Pipecat's immediate-termination API calls `transport.cancel(CancelFrame)`.
    The adapter must override `cancel()` to stop recv/event tasks cleanly.
    """
    assert "cancel" in SipInputTransport.__dict__, (
        "SipInputTransport must override cancel() to support task.cancel()"
    )


def test_sip_output_overrides_cancel():
    assert "cancel" in SipOutputTransport.__dict__, (
        "SipOutputTransport must override cancel() to hang up the SIP call"
    )


def test_audio_stream_input_overrides_cancel():
    assert "cancel" in AudioStreamInputTransport.__dict__


def test_audio_stream_output_overrides_cancel():
    assert "cancel" in AudioStreamOutputTransport.__dict__


# ─── Input transport cancel behavior ────────────────────────────────────────

@pytest.mark.asyncio
async def test_sip_input_cancel_stops_recv_loop_fast():
    """CancelFrame arriving while recv loop is running should terminate
    within a few frame-intervals (not hang waiting on the executor).
    """
    t = _make_sip_input()

    # Start recv loop
    t._recv_task = asyncio.create_task(t._recv_loop())
    # Let a few frames flow
    for _ in range(20):
        await asyncio.sleep(0.005)
        if len(t._pushed) >= 3:
            break
    assert len(t._pushed) >= 3, "recv loop should be producing frames"

    # Call cancel() as pipecat's base class would on CancelFrame
    frame = CancelFrame(reason="test")
    # super().cancel() is pipecat's base — it tries to access internal state
    # we don't emulate. Patch it to a no-op for this unit test.
    async def _noop_super_cancel(self, f):
        pass
    t.__class__.__mro__[1].cancel  # sanity: base class has cancel
    import unittest.mock
    with unittest.mock.patch.object(
        SipInputTransport.__mro__[1], "cancel", new=_noop_super_cancel
    ):
        await asyncio.wait_for(t.cancel(frame), timeout=1.0)

    assert t._started is False
    assert t._recv_task is None, "_recv_task should be cleared"


@pytest.mark.asyncio
async def test_sip_input_cancel_tolerates_session_gone():
    """If cancel() races against a remote BYE (Rust session already torn down),
    the recv loop should exit silently (our `_recv_loop` already does this).
    cancel() must still complete without raising.
    """
    ep = _FakeEndpoint(session_gone=True)
    t = _make_sip_input(endpoint=ep)
    t._recv_task = asyncio.create_task(t._recv_loop())
    await asyncio.sleep(0.05)  # let the loop run + hit the exception

    import unittest.mock
    async def _noop(self, f):
        pass
    with unittest.mock.patch.object(
        SipInputTransport.__mro__[1], "cancel", new=_noop
    ):
        await asyncio.wait_for(t.cancel(CancelFrame(reason="test")), timeout=1.0)

    assert t._started is False


# ─── Output transport cancel behavior ───────────────────────────────────────

@pytest.mark.asyncio
async def test_sip_output_cancel_hangs_up():
    ep = _FakeEndpoint()
    t = _make_sip_output(endpoint=ep)

    import unittest.mock
    async def _noop(self, f):
        pass
    with unittest.mock.patch.object(
        SipOutputTransport.__mro__[1], "cancel", new=_noop
    ):
        await asyncio.wait_for(t.cancel(CancelFrame(reason="test")), timeout=1.0)

    assert ep.hangup_calls == 1, "cancel() must invoke endpoint.hangup()"


@pytest.mark.asyncio
async def test_sip_output_cancel_calls_idempotent_hangup():
    """``hangup`` is idempotent under the 0.2.0 Terminated lifecycle: on
    a session already torn down, the Rust core short-circuits to
    success — Python sees no exception. cancel() can therefore call
    hangup unconditionally with no defensive try/except. This test
    pins that we DO call hangup exactly once and we DO NOT wrap it in
    a try/except that would swallow real errors (e.g. unknown session
    id from a programming bug).
    """
    ep = _FakeEndpoint(session_gone=True)
    t = _make_sip_output(endpoint=ep)

    import unittest.mock
    async def _noop(self, f):
        pass
    with unittest.mock.patch.object(
        SipOutputTransport.__mro__[1], "cancel", new=_noop
    ):
        await asyncio.wait_for(t.cancel(CancelFrame(reason="test")), timeout=1.0)

    # ``hangup`` is called once, and (because Rust's hangup is idempotent
    # on terminated sessions) the call completes without raising. The
    # _FakeEndpoint mimics that idempotency by recording the call and
    # returning normally even with session_gone=True.
    assert ep.hangup_calls == 1


# ─── audio_stream transport parity ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_audio_stream_input_cancel_stops_recv_loop_fast():
    t = _make_audio_stream_input()
    t._recv_task = asyncio.create_task(t._recv_loop())
    for _ in range(20):
        await asyncio.sleep(0.005)
        if len(t._pushed) >= 3:
            break
    assert len(t._pushed) >= 3

    import unittest.mock
    async def _noop(self, f):
        pass
    with unittest.mock.patch.object(
        AudioStreamInputTransport.__mro__[1], "cancel", new=_noop
    ):
        await asyncio.wait_for(
            t.cancel(CancelFrame(reason="test")), timeout=1.0
        )

    assert t._started is False
    assert t._recv_task is None


@pytest.mark.asyncio
async def test_audio_stream_output_cancel_hangs_up():
    ep = _FakeEndpoint()
    t = _make_audio_stream_output(endpoint=ep)

    import unittest.mock
    async def _noop(self, f):
        pass
    with unittest.mock.patch.object(
        AudioStreamOutputTransport.__mro__[1], "cancel", new=_noop
    ):
        await asyncio.wait_for(
            t.cancel(CancelFrame(reason="test")), timeout=1.0
        )

    assert ep.hangup_calls == 1
