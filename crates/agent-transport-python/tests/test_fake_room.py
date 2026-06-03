"""Tests for ``_FakeRoom`` / ``_FakeLocalParticipant`` stubs.

These satisfy the small surface area LiveKit's ``_ParticipantAudioOutput``
touches via ``self._room`` (``local_participant.publish_track``,
``isconnected()``). Our ``TransportAudioOutput`` overrides
``_publish_track`` so the ``publish_track`` call path is dead code in
production — the tests pin the stub's behaviour so a refactor doesn't
silently break our expectations.

LiveKit 1.5.9 removed the ``reconnected``-listener path on
``_ParticipantAudioOutput``, so the corresponding ``on``/``off`` stub
methods (and their tests) are gone.
"""

from __future__ import annotations

import pytest

from agent_transport.sip.livekit._fake_room import (
    _FakeLocalParticipant,
    _FakeRoom,
)


def test_fake_local_participant_has_required_attrs():
    """``_ParticipantAudioOutput._publish_track`` reads
    ``room.local_participant.publish_track`` and the transcription path
    reads ``identity`` / ``attributes`` / ``track_publications``.
    """
    lp = _FakeLocalParticipant(identity="test-agent")
    assert lp.identity == "test-agent"
    assert lp.attributes == {}
    assert lp.track_publications == {}


def test_fake_local_participant_default_identity():
    lp = _FakeLocalParticipant()
    assert lp.identity == "agent"


@pytest.mark.asyncio
async def test_fake_local_participant_publish_track_is_safe_noop():
    """Pattern A's ``TransportAudioOutput._publish_track`` short-circuits,
    so this path shouldn't execute. But if it did (e.g., LiveKit adds a
    second publish call we don't override), it must NOT raise.
    """
    lp = _FakeLocalParticipant()
    result = await lp.publish_track(track=None, options=None)
    assert result is None


def test_fake_room_default_identity():
    room = _FakeRoom()
    assert room.local_participant.identity == "agent"


def test_fake_room_custom_identity():
    room = _FakeRoom(identity="my-bot")
    assert room.local_participant.identity == "my-bot"


def test_fake_room_isconnected_is_false():
    """LiveKit's transcription publish path is guarded by
    ``if self._room.isconnected(): ...``. We're not connected to a real
    WebRTC room, so transcription publishes are short-circuited.
    """
    room = _FakeRoom()
    assert room.isconnected() is False


