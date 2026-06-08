"""agent_id is optional; observability upload is skipped (not crashed) without it.

The server no longer raises when ``agent_id`` is unset — it runs fine. But obs
keys sessions on agent_id (the sessions table is NOT NULL), so when
``AGENT_OBSERVABILITY_URL`` is configured *and* agent_id is missing,
``finalize_session`` must skip the upload (and judges) with a warning and KEEP
the local recording, rather than write an unparented session.
"""

import asyncio
import logging
import os

import pytest

from agent_transport.sip.livekit import _session_finalize
from agent_transport.sip.livekit import observability


class _FakeSession:
    usage = None
    stt = None
    tts = None
    llm = None

    def on(self, event, cb):
        # Fire 'close' synchronously so finalize's close-wait resolves instantly
        # (no 5s timeout) without needing a real AgentSession.
        if event == "close":
            cb()

    async def aclose(self):
        pass


class _FakeRoom:
    _remote = None  # skips the participant_disconnected emit branch


class _FakeCtx:
    def __init__(self):
        self._session = _FakeSession()
        self._room = _FakeRoom()
        self.account_id = None
        self.direction = "inbound"
        self.metadata = {}
        self.tagger = None
        self.evaluation = None

    async def _run_shutdown_callbacks(self, reason):
        pass


class _FakeEndpoint:
    def stop_recording(self, session_id):
        pass


@pytest.mark.parametrize("agent_id, expect_upload", [("", False), ("agent-9", True)])
@pytest.mark.asyncio
async def test_finalize_skips_upload_without_agent_id(agent_id, expect_upload, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("AGENT_OBSERVABILITY_URL", "https://obs.example/v0")

    calls = {"upload": 0, "judges": 0}

    async def _fake_upload(*a, **k):
        calls["upload"] += 1

    async def _fake_judges(*a, **k):
        calls["judges"] += 1

    # upload_session_report is imported lazily inside finalize_session from
    # .observability; run_configured_judges is bound at module load.
    monkeypatch.setattr(observability, "upload_session_report", _fake_upload, raising=True)
    monkeypatch.setattr(_session_finalize, "run_configured_judges", _fake_judges, raising=True)

    # A recording file that already exists — finalize removes it only when an
    # upload was actually attempted.
    rec = tmp_path / "recording_x.ogg"
    rec.write_bytes(b"ogg")

    ctx = _FakeCtx()
    with caplog.at_level(logging.WARNING):
        await _session_finalize.finalize_session(
            ctx,
            session_id="sess-x",
            endpoint=_FakeEndpoint(),
            transport="sip",
            agent_name="sip-agent",
            agent_id=agent_id,
            recording_path=str(rec),
            recording_started_at=None,
            reason="call ended",
            logger=logging.getLogger("test"),
        )

    assert calls["upload"] == (1 if expect_upload else 0)
    assert calls["judges"] == (1 if expect_upload else 0)
    if expect_upload:
        assert not rec.exists(), "recording should be cleaned up after a real upload attempt"
    else:
        assert rec.exists(), "recording must be kept when upload is skipped for missing agent_id"
        assert any("agent_id is unset" in r.message for r in caplog.records), \
            "a warning must explain why the upload was skipped"
