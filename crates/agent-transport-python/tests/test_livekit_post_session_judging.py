from types import SimpleNamespace

import pytest

from agent_transport.sip.livekit._room_facade import TransportRoom, _StubJobContext
from agent_transport.sip.livekit.audio_stream_server import JobContext as AudioStreamJobContext
from agent_transport.sip.livekit.judging import EvaluationConfig, run_configured_judges
from agent_transport.sip.livekit.observability import _ensure_transport_tags
from agent_transport.sip.livekit.server import JobContext as SipJobContext
from livekit.agents.evals import JudgmentResult


class FakeEndpoint:
    def __init__(self):
        self.input_sample_rate = 8000

    def stop_recording(self, session_id):
        pass


class FakeHistory:
    def copy(self):
        return self


class FakeLogger:
    def __init__(self):
        self.warnings = []
        self.infos = []

    def warning(self, *args, **kwargs):
        self.warnings.append((args, kwargs))

    def info(self, *args, **kwargs):
        self.infos.append((args, kwargs))


class FixedJudge:
    def __init__(self, verdict="pass"):
        self._verdict = verdict

    @property
    def name(self):
        return "fixed"

    async def evaluate(self, *, chat_ctx, reference=None, llm=None):
        return JudgmentResult(
            verdict=self._verdict,
            reasoning=f"fixed {self._verdict}",
            instructions="deterministic test judge",
        )


class FakeTagger:
    def __init__(self):
        self.added = []
        self.metadata = {}

    def add(self, name, *, metadata=None):
        self.added.append(name)
        self.metadata[name] = metadata


def _make_ctx():
    room = TransportRoom(
        endpoint=FakeEndpoint(),
        session_id="call-1",
        agent_name="agent",
        caller_identity="sip:caller@x",
    )
    return _StubJobContext(room=room, agent_name="agent")


def _make_session():
    return SimpleNamespace(
        _recorder_io=None,
        _recording_options={"audio": False, "traces": False, "logs": True, "transcript": True},
        options=SimpleNamespace(),
        _started_at=123.0,
        _recorded_events=[],
        history=FakeHistory(),
        usage=SimpleNamespace(model_usage=[]),
    )


def test_transport_tags_use_generic_account_names():
    tagger = FakeTagger()

    _ensure_transport_tags(
        tagger,
        account_id="acct-1",
        transport="sip",
        direction="inbound",
        agent_id="agent-9",
        agent_name="support-agent",
    )

    assert tagger.added == [
        "agent.session",
        "agent_id:agent-9",
        "agent_name:support-agent",
        "account_id:acct-1",
        "transport:sip",
        "direction:inbound",
    ]
    assert tagger.metadata["agent.session"] == {
        "agent_id": "agent-9",
        "agent_name": "support-agent",
        "account_id": "acct-1",
        "transport": "sip",
        "direction": "inbound",
    }
    assert tagger.metadata["account_id:acct-1"] == {"account_id": "acct-1"}


def test_sip_job_context_set_metadata_adds_observability_tags():
    tagger = FakeTagger()
    ctx = SipJobContext(
        session_id="call-1",
        remote_uri="sip:caller@example.com",
        direction="inbound",
        endpoint=FakeEndpoint(),
    )
    ctx._agent_name = "support-agent"
    ctx._agent_id = "agent-9"
    ctx._tagger = tagger

    ctx.set_metadata({"account_id": "acct-1", "customer_tier": "gold", "empty": None})

    assert ctx.account_id == "acct-1"
    assert ctx.metadata == {"account_id": "acct-1", "customer_tier": "gold"}
    assert tagger.added == [
        "agent.session",
        "agent_id:agent-9",
        "agent_name:support-agent",
        "account_id:acct-1",
        "transport:sip",
        "direction:inbound",
    ]
    assert tagger.metadata["agent.session"] == {
        "account_id": "acct-1",
        "customer_tier": "gold",
        "agent_id": "agent-9",
        "agent_name": "support-agent",
        "transport": "sip",
        "direction": "inbound",
    }


def test_audio_stream_job_context_set_metadata_adds_observability_tags():
    tagger = FakeTagger()
    ctx = AudioStreamJobContext(
        session_id="stream-1",
        plivo_call_uuid="call-1",
        stream_id="stream-1",
        direction="inbound",
        extra_headers={},
        endpoint=FakeEndpoint(),
    )
    ctx._agent_name = "support-agent"
    ctx._agent_id = "agent-9"
    ctx._tagger = tagger

    ctx.set_metadata({"account_id": "acct-1"})

    assert ctx.account_id == "acct-1"
    assert tagger.added == [
        "agent.session",
        "agent_id:agent-9",
        "agent_name:support-agent",
        "account_id:acct-1",
        "transport:audio_stream",
        "direction:inbound",
    ]


@pytest.mark.asyncio
async def test_configured_judges_attach_evaluations_and_success_outcome():
    ctx = _make_ctx()

    ran = await run_configured_judges(
        session=_make_session(),
        job_context=ctx,
        evaluation=EvaluationConfig(
            judge_llm=object(),
            judges=[FixedJudge("pass")],
        ),
        session_id="call-1",
        logger=FakeLogger(),
    )

    assert ran is True
    assert ctx.tagger.evaluations == [
        {
            "name": "fixed",
            "tag": "lk.judge.fixed:pass",
            "verdict": "pass",
            "reasoning": "fixed pass",
            "instructions": "deterministic test judge",
        }
    ]
    assert ctx.tagger.outcome == "success"


@pytest.mark.asyncio
async def test_configured_judges_mark_failure_outcome_when_any_judge_fails():
    ctx = _make_ctx()

    ran = await run_configured_judges(
        session=_make_session(),
        job_context=ctx,
        evaluation=EvaluationConfig(
            judge_llm=object(),
            judges=[FixedJudge("fail")],
        ),
        session_id="call-1",
        logger=FakeLogger(),
    )

    assert ran is True
    assert ctx.tagger.evaluations[0]["verdict"] == "fail"
    assert ctx.tagger.outcome == "fail"


@pytest.mark.asyncio
async def test_configured_judges_skip_without_judge_llm():
    ctx = _make_ctx()
    logger = FakeLogger()

    ran = await run_configured_judges(
        session=_make_session(),
        job_context=ctx,
        evaluation=EvaluationConfig(
            judge_llm=None,
            judges=[FixedJudge("pass")],
        ),
        session_id="call-1",
        logger=logger,
    )

    assert ran is False
    assert ctx.tagger.evaluations == []
    assert ctx.tagger.outcome is None
    assert logger.warnings


@pytest.mark.asyncio
async def test_configured_judges_skip_without_evaluation_config():
    ctx = _make_ctx()

    ran = await run_configured_judges(
        session=_make_session(),
        job_context=ctx,
        evaluation=None,
        session_id="call-1",
        logger=FakeLogger(),
    )

    assert ran is False
    assert ctx.tagger.evaluations == []
    assert ctx.tagger.outcome is None


@pytest.mark.asyncio
async def test_configured_judges_can_skip_auto_outcome():
    ctx = _make_ctx()

    ran = await run_configured_judges(
        session=_make_session(),
        job_context=ctx,
        evaluation=EvaluationConfig(
            judge_llm=object(),
            judges=[FixedJudge("pass")],
            auto_outcome=False,
        ),
        session_id="call-1",
        logger=FakeLogger(),
    )

    assert ran is True
    assert ctx.tagger.evaluations[0]["verdict"] == "pass"
    assert ctx.tagger.outcome is None
