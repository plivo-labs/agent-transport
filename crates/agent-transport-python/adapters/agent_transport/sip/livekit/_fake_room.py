"""Minimal ``rtc.Room``-shape stubs for Pattern-A inheritance.

LiveKit's :class:`~livekit.agents.voice.room_io._output._ParticipantAudioOutput`
references ``self._room`` from a handful of places — chiefly:

* ``self._room.local_participant.publish_track(track, options)`` and
  ``await self._publication.wait_for_subscription()`` inside
  ``_publish_track``.
* ``self._room.on("reconnected", ...)`` / ``off("reconnected", ...)`` in
  ``start``/``aclose``.

Our :class:`TransportAudioOutput` overrides ``_publish_track`` so the
``publish_track`` call never happens; the room is only used by the
``on``/``off`` reconnection hooks. We just need stubs that satisfy the
attribute access without doing anything.

Why not pass the real ``TransportRoom`` (our agent-facing facade)?
``TransportRoom`` is wired to ``ctx.room``, fires per-call lifecycle
events to user code, and would fan ``reconnected`` listeners to nothing
meaningful — its protocol is for the agent author, not the
``_ParticipantAudioOutput``. Keeping the two cleanly separated avoids
accidental cross-coupling: if LiveKit adds new internal room access in a
future ``_ParticipantAudioOutput`` patch, we want to fail loudly in
review rather than silently routing it through ``TransportRoom``.
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

    * :meth:`on` / :meth:`off` — ``_ParticipantAudioOutput.start``
      registers ``reconnected`` listener; ``aclose`` unregisters it. We
      no-op both — there's no underlying WebRTC connection to reconnect
      to.
    * :attr:`local_participant` — used inside ``_publish_track`` (which
      we override) and ``_publish_transcription``.
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
