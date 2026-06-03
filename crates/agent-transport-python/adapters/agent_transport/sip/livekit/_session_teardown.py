"""Session-teardown helpers for the LiveKit adapter.

Mirrors the Node side (``_session_teardown.ts``). Kept out of the pipecat
adapters because Pipecat has no ``AgentSession`` equivalent.

VERSION COUPLING:
This module writes private LiveKit ``AgentSession`` / ``AgentActivity`` state
to close a race between Plivo-side disconnect and STT-transcript delivery.
The fields are verified against ``livekit-agents == 1.5.x`` (see
``pyproject.toml`` ``[livekit]`` extra). If upstream renames or removes
``_closing`` / ``_scheduling_paused``, this helper silently becomes a no-op
(the writes are wrapped in ``try/except``) and the race in issue #83 returns
-- an integration test against the pinned version is the guardrail.
"""

import asyncio
import logging
from typing import Any

_logger = logging.getLogger("agent_transport.session_teardown")


def force_shutdown_agent_session(session: Any, background_tasks: set[asyncio.Task]) -> None:
    """Synchronously begin tearing down a LiveKit AgentSession on call termination.

    Pauses activity scheduling so buffered STT transcripts are dropped before
    they trigger LLM/TTS on a dead call, sets ``_closing``, closes the audio
    input to stop feeding STT, clears pending playout, and schedules the async
    ``shutdown``.

    RoomIO's ``_close_soon()`` is a fire-and-forget ``asyncio.Task`` — without
    flipping the scheduling guard synchronously here, a transcript that
    arrives before the close task pauses the activity would still reach the
    pipeline.
    """
    if session is None:
        return

    try:
        session._closing = True
    except Exception:
        _logger.debug("force_shutdown: failed to set _closing", exc_info=True)

    try:
        for activity in (
            getattr(session, "_activity", None),
            getattr(session, "_next_activity", None),
        ):
            if activity is not None:
                activity._scheduling_paused = True
    except Exception:
        _logger.debug("force_shutdown: failed to pause activity scheduling", exc_info=True)

    try:
        audio_in = getattr(session.input, "audio", None)
        if audio_in is not None:
            t = asyncio.create_task(audio_in.aclose())
            background_tasks.add(t)
            t.add_done_callback(background_tasks.discard)
    except Exception:
        _logger.debug("force_shutdown: failed to close audio input", exc_info=True)

    try:
        audio_out = getattr(session.output, "audio", None)
        if audio_out is not None and hasattr(audio_out, "clear_buffer"):
            audio_out.clear_buffer()
    except Exception:
        _logger.debug("force_shutdown: failed to clear audio output", exc_info=True)

    try:
        session.shutdown(drain=False)
    except Exception:
        _logger.debug("force_shutdown: shutdown() raised", exc_info=True)
