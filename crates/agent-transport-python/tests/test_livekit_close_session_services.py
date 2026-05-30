"""Tests for close_session_services — the helper that closes user-supplied
STT/TTS/LLM at session end so vendor WebSockets do not leak across calls.

Agent-transport's LiveKit adapter runs every call in-process, so we can't
rely on upstream livekit-agents' "subprocess exits, OS reclaims FDs"
strategy. close_session_services calls aclose() on session.stt/tts/llm
with per-service timeout and isolated error handling.

The attribute path (session.stt / session.tts / session.llm) and the
canonical close method (aclose()) were verified against the installed
livekit-agents 1.5.15: AgentSession exposes stt/tts/llm as public
properties returning self._stt/_tts/_llm, and the STT/TTS/LLM/RealtimeModel
base classes all expose async aclose() (no-ops that plugins override to
drop vendor sockets). AgentSession._aclose_impl does NOT call aclose() on
any of them.
"""

import asyncio
import importlib.util
import logging
import pathlib

import pytest


# Load _aio_utils directly from source so the test works against the
# worktree without needing the Rust extension built or the package
# re-installed in editable mode.
_AIO_UTILS_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "adapters"
    / "agent_transport"
    / "sip"
    / "livekit"
    / "_aio_utils.py"
)
_spec = importlib.util.spec_from_file_location("_aio_utils_under_test", _AIO_UTILS_PATH)
_aio_utils = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_aio_utils)
close_session_services = _aio_utils.close_session_services


class _FakeService:
    def __init__(self, *, raises: BaseException | None = None, sleep_s: float = 0.0):
        self.closed = False
        self.close_calls = 0
        self._raises = raises
        self._sleep_s = sleep_s

    async def aclose(self):
        self.close_calls += 1
        if self._sleep_s:
            await asyncio.sleep(self._sleep_s)
        if self._raises is not None:
            raise self._raises
        self.closed = True


class _FakeSession:
    def __init__(self, *, stt=None, tts=None, llm=None, vad=None):
        self.stt = stt
        self.tts = tts
        self.llm = llm
        # vad is process-scoped (shared across calls via proc.userdata['vad'])
        # and must NEVER be closed by close_session_services.
        self.vad = vad


@pytest.mark.asyncio
async def test_closes_all_three():
    stt, tts, llm = _FakeService(), _FakeService(), _FakeService()
    session = _FakeSession(stt=stt, tts=tts, llm=llm)

    await close_session_services(session)

    assert stt.close_calls == 1 and stt.closed
    assert tts.close_calls == 1 and tts.closed
    assert llm.close_calls == 1 and llm.closed


@pytest.mark.asyncio
async def test_one_failure_does_not_block_others(caplog):
    stt = _FakeService()
    tts = _FakeService(raises=RuntimeError("vendor 500"))
    llm = _FakeService()
    session = _FakeSession(stt=stt, tts=tts, llm=llm)

    caplog.set_level(logging.WARNING, logger="test")
    await close_session_services(session, logger=logging.getLogger("test"))

    assert stt.close_calls == 1 and stt.closed
    assert tts.close_calls == 1 and not tts.closed
    assert llm.close_calls == 1 and llm.closed
    assert any("tts" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_per_service_timeout():
    fast = _FakeService()
    slow = _FakeService(sleep_s=10.0)
    other = _FakeService()
    session = _FakeSession(stt=fast, tts=slow, llm=other)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await close_session_services(session, per_service_timeout=0.05)
    elapsed = loop.time() - started

    assert fast.closed
    assert not slow.closed  # timed out
    assert other.closed
    # fast + slow timeout + other; well under 1s
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_handles_none_and_missing_aclose():
    class _NoAclose:
        pass

    session = _FakeSession(stt=None, tts=_NoAclose(), llm=_FakeService())

    # Should not raise even though tts has no aclose and llm has no error.
    await close_session_services(session)

    assert session.llm.closed


@pytest.mark.asyncio
async def test_no_session_is_noop():
    # Defensive: closing a None session must not raise.
    await close_session_services(None)


@pytest.mark.asyncio
async def test_missing_attrs_are_noop():
    class _Bare:
        pass

    # Session with no stt/tts/llm attributes at all.
    await close_session_services(_Bare())


@pytest.mark.asyncio
async def test_vad_is_never_closed():
    """The process-scoped VAD (proc.userdata['vad'], shared across calls) must
    survive call teardown. close_session_services walks only stt/tts/llm and
    must never touch session.vad — closing it would break every other in-flight
    and future call in the same process."""
    stt, tts, llm = _FakeService(), _FakeService(), _FakeService()
    vad = _FakeService()
    session = _FakeSession(stt=stt, tts=tts, llm=llm, vad=vad)

    await close_session_services(session)

    assert stt.close_calls == 1 and tts.close_calls == 1 and llm.close_calls == 1
    # The invariant: VAD is left completely untouched.
    assert vad.close_calls == 0 and not vad.closed


def test_real_agent_session_exposes_service_attrs():
    """Lock the attribute contract against SDK drift: a real livekit-agents
    AgentSession must expose stt/tts/llm/vad as readable attributes (the exact
    names close_session_services walks). If a future SDK renames these, this
    fails loudly instead of the helper silently closing nothing."""
    agents = pytest.importorskip("livekit.agents")
    session = agents.AgentSession()
    for attr in ("stt", "tts", "llm", "vad"):
        assert hasattr(session, attr), f"AgentSession lost .{attr} attribute"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
