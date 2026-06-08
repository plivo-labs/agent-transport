"""Recording start is gated on observability being configured.

A recording's only purpose is to be uploaded as part of the session report
(``finalize_session`` uploads then deletes it). Without observability there's
nowhere to send it and nothing would clean it up, so ``_start_session_recording``
on ``AgentServerBase`` must not start one. This is the single source of the
"record iff we'll upload" policy — the symmetric counterpart to
``finalize_session`` (see test_observability_skip_without_agent_id).
"""

import logging

import pytest

from agent_transport.sip.livekit._server_base import AgentServerBase


class _FakeEndpoint:
    def __init__(self):
        self.started = []

    def start_recording(self, session_id, path, stereo):
        self.started.append((session_id, path, stereo))


class _Stub:
    """Minimal carrier of the attributes ``_start_session_recording`` reads —
    avoids constructing a full server (threads, load monitor, HTTP)."""

    _start_session_recording = AgentServerBase._start_session_recording

    def __init__(self, ep, recording, recording_dir):
        self._ep = ep
        self._recording = recording
        self._recording_dir = recording_dir
        self._recording_stereo = True
        self._logger = logging.getLogger("test")


def test_records_when_observability_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OBSERVABILITY_URL", "https://obs.example/v0")
    ep = _FakeEndpoint()
    stub = _Stub(ep, recording=True, recording_dir=str(tmp_path))

    rec_path, started_at = stub._start_session_recording("sess-1")

    assert rec_path == str(tmp_path / "recording_sess-1.ogg")
    assert started_at is not None
    assert ep.started == [("sess-1", rec_path, True)]


def test_skips_when_observability_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_OBSERVABILITY_URL", raising=False)
    ep = _FakeEndpoint()
    stub = _Stub(ep, recording=True, recording_dir=str(tmp_path))

    rec_path, started_at = stub._start_session_recording("sess-1")

    assert (rec_path, started_at) == (None, None)
    assert ep.started == [], "must not record when there's nowhere to upload"


def test_skips_when_recording_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OBSERVABILITY_URL", "https://obs.example/v0")
    ep = _FakeEndpoint()
    stub = _Stub(ep, recording=False, recording_dir=str(tmp_path))

    rec_path, started_at = stub._start_session_recording("sess-1")

    assert (rec_path, started_at) == (None, None)
    assert ep.started == []


def test_start_failure_is_swallowed(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("AGENT_OBSERVABILITY_URL", "https://obs.example/v0")

    class _BoomEndpoint:
        def start_recording(self, *a):
            raise RuntimeError("rust recorder unavailable")

    stub = _Stub(_BoomEndpoint(), recording=True, recording_dir=str(tmp_path))

    with caplog.at_level(logging.WARNING):
        rec_path, started_at = stub._start_session_recording("sess-1")

    assert (rec_path, started_at) == (None, None), \
        "a failed start must not leave a dangling path for finalize to upload/delete"
    assert any("Failed to start recording" in r.message for r in caplog.records)
