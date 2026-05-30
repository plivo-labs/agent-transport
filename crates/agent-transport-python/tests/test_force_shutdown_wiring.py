"""Wiring test for the #83 force-shutdown call site in ``_dispatch_event``.

``force_shutdown_agent_session`` is unit-tested in isolation
(``test_force_shutdown_agent_session.py``). This test locks the *wiring*: that
the ``participant_disconnected`` branch of ``_dispatch_event`` actually looks
up the session context and force-shuts-down the live AgentSession **before**
it sets the call-ended event — the exact ordering the #83 fix depends on.
Without it, a refactor could silently unwire the helper and the issue-#83 race
(a buffered STT transcript driving a wasted LLM+TTS turn on a dead call) would
return with every other test still green.

It drives the real ``_dispatch_event`` with a duck-typed ``FfiEvent`` (the
handler only calls ``WhichOneof(...)`` + reads plain attrs), so no Rust event
types are needed. Covers both servers (SIP ``AgentServer`` and
``AudioStreamServer``), whose context / ended-event maps are named differently.
"""

from types import SimpleNamespace

import pytest


class _FakeAgentSession:
    """Minimal stand-in satisfying ``force_shutdown_agent_session``.

    No ``_activity``/``audio`` so the helper takes its no-op branches and the
    only observable effects are ``_closing`` and the ``shutdown(drain=...)``
    call — exactly the two synchronous force-drop signals we assert on.
    """

    def __init__(self):
        self._closing = False
        self._activity = None
        self._next_activity = None
        self.input = SimpleNamespace(audio=None)
        self.output = SimpleNamespace(audio=None)
        self.shutdown_drain = "unset"  # records the drain kwarg

    def shutdown(self, *, drain=True):
        self.shutdown_drain = drain


def _fake_pd_event(session_id):
    """Duck-typed FfiEvent for a ``participant_disconnected`` room event."""
    pd = SimpleNamespace(session_id=session_id, reason="test")
    room_event = SimpleNamespace(
        room_handle="room-handle",
        participant_disconnected=pd,
        WhichOneof=lambda which: "participant_disconnected",
    )
    return SimpleNamespace(
        room_event=room_event,
        WhichOneof=lambda which: "room_event",
    )


# (class name, context-map attr, ended-events-map attr)
_SERVERS = [
    ("AgentServer", "_call_contexts", "_call_ended_events"),
    ("AudioStreamServer", "_session_contexts", "_session_ended_events"),
]


def _load(cls_name):
    if cls_name == "AgentServer":
        from agent_transport.sip.livekit.server import AgentServer

        return AgentServer
    from agent_transport.sip.livekit.audio_stream_server import AudioStreamServer

    return AudioStreamServer


def _bare_server(cls_name, ctx_attr, ended_attr, session_id, ctx):
    """A server instance with only the attrs ``_dispatch_event`` reads on the
    ``participant_disconnected`` wake branch."""
    cls = _load(cls_name)
    srv = cls.__new__(cls)
    srv._ep = SimpleNamespace(clear_buffer=lambda sid: None)
    srv._background_tasks = set()
    setattr(srv, ctx_attr, {session_id: ctx} if ctx is not None else {})

    class _Ended:
        def __init__(self):
            self.set_called = False

        def set(self):
            self.set_called = True

    ended = _Ended()
    setattr(srv, ended_attr, {session_id: ended})
    return srv, ended


@pytest.mark.parametrize("cls_name,ctx_attr,ended_attr", _SERVERS)
def test_dispatch_force_shuts_down_then_sets_ended_event(cls_name, ctx_attr, ended_attr):
    sid = "call-1"
    sess = _FakeAgentSession()
    ctx = SimpleNamespace(_session=sess)
    srv, ended = _bare_server(cls_name, ctx_attr, ended_attr, sid, ctx)

    result = srv._dispatch_event(_fake_pd_event(sid))

    assert result is False
    # Helper fired synchronously on the wake branch...
    assert sess._closing is True
    assert sess.shutdown_drain is False  # drain=False force-drop, not a graceful drain
    # ...and the call-ended event was still set (so _run_call/_run_session wakes).
    assert ended.set_called is True


@pytest.mark.parametrize("cls_name,ctx_attr,ended_attr", _SERVERS)
def test_dispatch_missing_ctx_is_safe_and_still_wakes(cls_name, ctx_attr, ended_attr):
    sid = "call-missing"
    srv, ended = _bare_server(cls_name, ctx_attr, ended_attr, sid, None)

    # No ctx registered for this session — must not raise, must still wake.
    result = srv._dispatch_event(_fake_pd_event(sid))

    assert result is False
    assert ended.set_called is True


@pytest.mark.parametrize("cls_name,ctx_attr,ended_attr", _SERVERS)
def test_dispatch_ctx_without_session_is_safe(cls_name, ctx_attr, ended_attr):
    sid = "call-2"
    ctx = SimpleNamespace(_session=None)
    srv, ended = _bare_server(cls_name, ctx_attr, ended_attr, sid, ctx)

    # ctx present but no AgentSession attached yet — must not raise, must wake.
    result = srv._dispatch_event(_fake_pd_event(sid))

    assert result is False
    assert ended.set_called is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
