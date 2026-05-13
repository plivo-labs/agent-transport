"""Tests for ``_FakeRoom`` / ``_FakeLocalParticipant`` stubs.

These satisfy the small surface area LiveKit's ``_ParticipantAudioOutput``
touches via ``self._room`` (``on`` / ``off`` listeners on ``"reconnected"``,
``local_participant.publish_track``, ``isconnected()``). Our
``TransportAudioOutput`` overrides ``_publish_track`` so the
``publish_track`` call path is dead code in production — these tests pin
the stub's behaviour so a refactor doesn't silently break the
reconnection listener wiring.
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


def test_fake_room_on_off_reconnected():
    """The base's ``start`` registers ``room.on("reconnected", ...)`` and
    ``aclose`` calls ``room.off("reconnected", ...)``. Both must succeed.
    """
    room = _FakeRoom()
    called = []

    def listener():
        called.append("fired")

    # on returns the listener (for decorator style)
    returned = room.on("reconnected", listener)
    assert returned is listener

    # off removes silently
    room.off("reconnected", listener)


def test_fake_room_off_unregistered_listener_is_noop():
    """LiveKit's ``rtc.Room.off`` is idempotent — calling it for a
    listener that was never registered should not raise. Our stub matches.
    """
    room = _FakeRoom()

    def listener():
        pass

    # Was never `on`-registered; off must not raise.
    room.off("reconnected", listener)


def test_fake_room_off_unknown_event_is_noop():
    """``off`` for an event name we never registered should be silent."""
    room = _FakeRoom()

    def listener():
        pass

    room.off("some-event-we-never-care-about", listener)


def test_fake_room_isconnected_is_false():
    """LiveKit's transcription publish path is guarded by
    ``if self._room.isconnected(): ...``. We're not connected to a real
    WebRTC room, so transcription publishes are short-circuited.
    """
    room = _FakeRoom()
    assert room.isconnected() is False


def test_fake_room_listener_dispatch_independence():
    """Multiple ``on`` registrations should each be tracked separately;
    ``off`` should remove only the one passed in. (Mirrors
    ``rtc.Room.on``/``off`` semantics.)
    """
    room = _FakeRoom()

    def a():
        pass

    def b():
        pass

    room.on("reconnected", a)
    room.on("reconnected", b)
    assert len(room._listeners["reconnected"]) == 2

    room.off("reconnected", a)
    assert len(room._listeners["reconnected"]) == 1
    assert room._listeners["reconnected"][0] is b
