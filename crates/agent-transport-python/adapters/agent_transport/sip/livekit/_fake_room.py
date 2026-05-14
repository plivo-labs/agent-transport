"""Minimal ``rtc.Room``-shape stubs for Pattern-A inheritance.

LiveKit's :class:`~livekit.agents.voice.room_io._output._ParticipantAudioOutput`
references ``self._room`` from a handful of places:

* ``self._room.local_participant.publish_track(track, options)`` +
  ``await self._publication.wait_for_subscription()`` inside
  ``_publish_track`` — we OVERRIDE ``_publish_track`` so this path
  never executes on our subclass.
* ``self._room.local_participant.identity`` / ``track_publications``
  inside the transcription paths.
* ``self._room.isconnected()`` as a guard on the transcription publish
  path — we return ``False`` so transcription short-circuits cleanly.

``on``/``off`` are kept as a safety net but are no longer registered
by :class:`_ParticipantAudioOutput` itself: LiveKit removed the
``reconnected`` listener and ``_republish_task`` in 1.5.9. Other
LiveKit classes (``_ParticipantInput``, ``_ParticipantTranscriptionOutput``,
``RoomIO``) do still call ``room.on``/``room.off``, but those classes
do not receive our :class:`_FakeRoom` — they receive the real
``rtc.Room`` constructed by the LiveKit framework. The stubs are
retained as cheap insurance in case a future LiveKit minor reintroduces
listener wiring on the audio-output path; the drift test would fail
loudly first.

Why not pass the real ``TransportRoom`` (our agent-facing facade)?
``TransportRoom`` is wired to ``ctx.room``, fires per-call lifecycle
events to user code, and would conflate audio-output internals with the
agent-author protocol. Keeping them cleanly separated avoids accidental
cross-coupling: if LiveKit adds new internal room access in a future
``_ParticipantAudioOutput`` patch, we want to fail loudly in review
rather than silently routing it through ``TransportRoom``.
"""

from __future__ import annotations

import asyncio
from typing import Any


class _FakeLocalParticipant:
    """Mirror of ``rtc.LocalParticipant`` for ``_ParticipantAudioOutput``.

    Only ``publish_track`` and the ``identity`` attribute are touched
    inside ``_ParticipantAudioOutput.publish_track`` and
    ``_publish_transcription``. We expose both as no-ops/empty.
    """

    __slots__ = ("identity", "attributes", "track_publications")

    def __init__(self, identity: str = "agent") -> None:
        self.identity = identity
        self.attributes: dict[str, str] = {}
        self.track_publications: dict[str, Any] = {}

    async def publish_track(self, track: Any, options: Any) -> Any:  # noqa: ARG002
        """Stub. ``TransportAudioOutput`` overrides ``_publish_track`` so
        this code path never executes — present only as a safety net
        (raising would be louder, but would mask any future LiveKit
        change that adds an alternate publish path)."""
        return None


class _FakeRoom:
    """Mirror of ``rtc.Room`` for ``_ParticipantAudioOutput``.

    Implements only the surface area touched by the base output:

    * :meth:`on` / :meth:`off` — kept as a safety net; the LiveKit
      ``reconnected``-listener path was removed in 1.5.9 (see module
      docstring).
    * :attr:`local_participant` — used inside ``_publish_track`` (which
      we override) and the transcription path.
    * :meth:`isconnected` — guards transcription publish; return False
      so the transcription path short-circuits cleanly (we surface
      transcripts through user-level handlers, not WebRTC data tracks).
    """

    __slots__ = ("local_participant", "_listeners")

    def __init__(self, identity: str = "agent") -> None:
        self.local_participant = _FakeLocalParticipant(identity)
        self._listeners: dict[str, list[Any]] = {}

    def on(self, event_name: str, callback: Any) -> Any:
        """Register a listener. Returns the callback so decorators work."""
        self._listeners.setdefault(event_name, []).append(callback)
        return callback

    def off(self, event_name: str, callback: Any) -> None:
        """Remove a listener. Idempotent — calling off for an unregistered
        listener is a no-op (matches rtc.Room behavior)."""
        listeners = self._listeners.get(event_name, [])
        try:
            listeners.remove(callback)
        except ValueError:
            pass

    def isconnected(self) -> bool:
        """We're not connected to a LiveKit WebRTC room. Transcription
        publish paths short-circuit when this returns False."""
        return False
