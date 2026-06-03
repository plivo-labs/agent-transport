"""Shared end-of-session finalize for the LiveKit adapters.

SIP (``server.py``) and audio_stream (``audio_stream_server.py``) run an
identical teardown sequence once the call ends; this is the single canonical
copy. Each server keeps only its transport-specific orchestration afterwards
(hangup style, active-session bookkeeping).

Ordering is load-bearing:
  1. Emit ``participant_disconnected`` from the caller's task — LiveKit's
     RoomIO listener synchronously schedules ``_close_soon``, whose task
     inherits the current ``_JobContextVar``.
  2. Wait for the session to close so in-flight LLM/TTS responses finalize
     into history BEFORE the recorder stops — otherwise the final agent turn
     is truncated from both the transcript and the OGG recording.
  3. Stop recording and wait for the Rust recorder to flush the file.
  4. Run post-session judges + upload the report BEFORE closing vendor
     services — judges invoke an LLM and must not race the socket teardown.
  5. Close vendor STT/TTS/LLM sockets (``aclose`` does not cascade to them).
  6. Fire shutdown callbacks once (deduped by ``_run_shutdown_callbacks``).
"""

import asyncio
import os
from typing import Any

from ._aio_utils import close_session_services
from .judging import run_configured_judges
from .observability import _get_observability_url


async def finalize_session(
    ctx: Any,
    *,
    session_id: str,
    endpoint: Any,
    transport: str,
    agent_name: str,
    agent_id: str,
    recording_path: str | None,
    recording_started_at: float | None,
    reason: str,
    logger: Any,
) -> None:
    # Emit ``participant_disconnected`` from THIS task's context (which has
    # ``_JobContextVar`` set). LiveKit's RoomIO listener synchronously calls
    # ``AgentSession._close_soon`` → ``asyncio.create_task(_aclose_impl(...))``;
    # the new task inherits the current context, so the close task can resolve
    # ``get_job_context()``.
    if ctx and ctx._room and getattr(ctx._room, "_remote", None):
        try:
            remote = ctx._room._remote
            remote.disconnect_reason = 1  # CLIENT_INITIATED
            ctx._room.emit("participant_disconnected", remote)
        except Exception:
            logger.exception("participant_disconnected emit failed")

    session = ctx._session
    if session is not None:
        try:
            usage = session.usage
            if usage and usage.model_usage:
                logger.info("Session %s usage: %s", session_id, usage)
        except Exception:
            pass

        # Wait for session close (triggered by participant_disconnected →
        # _close_soon) rather than calling aclose() directly, which cancels
        # in-progress LLM/TTS and discards the response from history. Give it a
        # few seconds to finalize in-flight responses.
        try:
            close_event = asyncio.Event()
            session.on("close", lambda *_: close_event.set())
            try:
                await asyncio.wait_for(close_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                await session.aclose()
        except Exception:
            pass

        # Stop recording and wait for the file to be finalized. Runs AFTER the
        # close wait so the final drained turn is captured. The Rust recorder
        # finalizes on a background thread; poll until the file appears (~2s).
        if recording_path:
            try:
                endpoint.stop_recording(session_id)
                for _ in range(20):
                    if os.path.exists(recording_path):
                        break
                    await asyncio.sleep(0.1)
            except Exception:
                pass

        # Upload session report after close so history is complete. Runs BEFORE
        # close_session_services because post-session judges invoke an LLM and
        # must not race the vendor socket teardown.
        obs_url = _get_observability_url()
        if obs_url:
            try:
                from .observability import upload_session_report

                await run_configured_judges(
                    session=session,
                    job_context=ctx,
                    evaluation=getattr(ctx, "evaluation", None),
                    session_id=session_id,
                    logger=logger,
                )
                await upload_session_report(
                    session, session_id, obs_url, agent_name,
                    recording_path, recording_started_at,
                    account_id=ctx.account_id,
                    agent_id=agent_id,
                    transport=transport,
                    direction=ctx.direction,
                    metadata=ctx.metadata,
                    tagger=ctx.tagger,
                    job_context=ctx,
                )
            except Exception:
                logger.warning("Failed to upload session report for session %s", session_id, exc_info=True)

            if recording_path:
                try:
                    os.remove(recording_path)
                except Exception:
                    logger.warning("Failed to clean up recording %s", recording_path, exc_info=True)

        # AgentSession.aclose() does NOT cascade-close the user-supplied
        # STT/TTS/LLM (verified against livekit-agents 1.5.x), so their vendor
        # WebSockets would leak per session on our long-lived in-process
        # server. Close them explicitly after the session has drained, before
        # shutdown callbacks / hangup. Never raises.
        await close_session_services(session, logger=logger)

    # Fire shutdown callbacks once (no-op if shutdown() already dispatched them
    # — _take_shutdown_callbacks() dedups).
    await ctx._run_shutdown_callbacks(reason)
