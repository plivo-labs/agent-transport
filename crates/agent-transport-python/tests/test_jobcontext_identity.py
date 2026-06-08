import pytest

from agent_transport.sip.livekit._room_facade import (
    TransportRoom,
    create_transport_context,
)
from agent_transport.sip.livekit.audio_stream_server import (
    JobContext as AudioStreamJobContext,
)
from agent_transport.sip.livekit.server import JobContext as SipJobContext, JobProcess


class FakeEndpoint:
    def __init__(self):
        self.input_sample_rate = 8000
        self.hangup_calls = []
        self.start_recording_calls = []
        self.stop_recording_calls = []

    def hangup(self, session_id):
        self.hangup_calls.append(session_id)

    def start_recording(self, session_id, path, stereo):
        self.start_recording_calls.append((session_id, path, stereo))

    def stop_recording(self, session_id):
        self.stop_recording_calls.append(session_id)


@pytest.mark.asyncio
async def test_sip_entrypoint_context_matches_get_job_context():
    from livekit.agents.job import _JobContextVar, get_job_context

    ep = FakeEndpoint()
    proc = JobProcess()
    room = TransportRoom(
        endpoint=ep,
        session_id="call-identity",
        agent_name="sip-agent",
        caller_identity="sip:caller@example.com",
    )
    ctx = SipJobContext(
        session_id="call-identity",
        remote_uri="sip:caller@example.com",
        direction="inbound",
        endpoint=ep,
        userdata={"k": "v"},
        _agent_name="sip-agent",
        _room=room,
        _proc=proc,
    )
    job_ctx, token = create_transport_context(
        room,
        agent_name="sip-agent",
        inference_executor="executor",
        context=ctx,
    )
    ctx._job_ctx_token = token

    try:
        async def entrypoint(entry_ctx):
            global_ctx = get_job_context()
            assert global_ctx is entry_ctx
            assert global_ctx is job_ctx
            assert global_ctx.endpoint is ep
            assert global_ctx.room is room
            assert global_ctx.job.room is room
            assert global_ctx.proc is proc
            assert global_ctx.inference_executor == "executor"

        await entrypoint(ctx)
    finally:
        _JobContextVar.reset(token)


@pytest.mark.asyncio
async def test_audio_stream_entrypoint_context_matches_get_job_context():
    from livekit.agents.job import _JobContextVar, get_job_context

    ep = FakeEndpoint()
    proc = JobProcess()
    room = TransportRoom(
        endpoint=ep,
        session_id="stream-identity",
        agent_name="audio-agent",
        caller_identity="plivo-call-uuid",
        remote_kind=0,
    )
    ctx = AudioStreamJobContext(
        session_id="stream-identity",
        plivo_call_uuid="plivo-call-uuid",
        stream_id="stream-uuid",
        direction="inbound",
        extra_headers={"x": "y"},
        endpoint=ep,
        userdata={"k": "v"},
        _agent_name="audio-agent",
        _room=room,
        _proc=proc,
    )
    job_ctx, token = create_transport_context(
        room,
        agent_name="audio-agent",
        inference_executor="executor",
        context=ctx,
    )
    ctx._job_ctx_token = token

    try:
        async def entrypoint(entry_ctx):
            global_ctx = get_job_context()
            assert global_ctx is entry_ctx
            assert global_ctx is job_ctx
            assert global_ctx.endpoint is ep
            assert global_ctx.room is room
            assert global_ctx.job.room is room
            assert global_ctx.proc is proc
            assert global_ctx.inference_executor == "executor"

        await entrypoint(ctx)
    finally:
        _JobContextVar.reset(token)
