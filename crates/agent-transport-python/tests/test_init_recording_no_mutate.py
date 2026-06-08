"""Regression tests for _StubJobContext.init_recording options restore.

The transport server owns Rust recording so it can pass the exact file to the
LiveKit SDK upload helper. The JobContext hook only prevents LiveKit RecorderIO
from starting a second Python-level recording.
"""

import pytest

from agent_transport.sip.livekit._room_facade import TransportRoom, _StubJobContext


class FakeEndpoint:
    def __init__(self):
        self.input_sample_rate = 8000
        self.started = []
        self.stopped = []

    def start_recording(self, session_id, path, stereo):
        self.started.append((session_id, path, stereo))

    def stop_recording(self, session_id):
        self.stopped.append(session_id)


def _make_ctx():
    ep = FakeEndpoint()
    room = TransportRoom(
        endpoint=ep, session_id="call-1",
        agent_name="agent", caller_identity="sip:caller@x",
    )
    return _StubJobContext(room=room, agent_name="agent"), room, ep


def test_init_recording_sets_audio_false_while_active():
    ctx, _, ep = _make_ctx()
    options = {"audio": True, "video": False, "text": True}

    ctx.init_recording(options)

    assert options["audio"] is False
    assert options["video"] is False
    assert options["text"] is True
    assert ep.started == []


@pytest.mark.asyncio
async def test_init_recording_restores_audio_on_session_end():
    ctx, _, _ = _make_ctx()
    options = {"audio": True}
    ctx.init_recording(options)
    assert options["audio"] is False

    await ctx._on_session_end()

    assert options["audio"] is True


@pytest.mark.asyncio
async def test_init_recording_restores_missing_audio_key():
    ctx, _, _ = _make_ctx()
    options = {"audio": True}
    ctx.init_recording(options)

    ctx._original_audio_recording_flag = None
    await ctx._on_session_end()

    assert "audio" not in options


def test_init_recording_noop_when_audio_disabled():
    ctx, _, ep = _make_ctx()
    options = {"audio": False}
    ctx.init_recording(options)
    assert options == {"audio": False}
    assert ep.started == []
