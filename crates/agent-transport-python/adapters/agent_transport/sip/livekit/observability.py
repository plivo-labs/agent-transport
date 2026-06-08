"""Observability upload integration for agent-transport.

The Python LiveKit SDK owns the native observability protocol: protobuf
recording uploads, OTLP log records, Tagger outcomes, and JudgeGroup results.
agent-transport only adapts its SIP/audio_stream JobContext into the SDK's
SessionReport shape, attaches transport tags through the SDK Tagger, and then
delegates upload to LiveKit's telemetry helpers.

Set AGENT_OBSERVABILITY_URL plus LIVEKIT_API_KEY / LIVEKIT_API_SECRET to enable
native SDK uploads. The API key/secret are shared with agent-observability so it
can validate the SDK Bearer JWT locally.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("agent_transport.observability")

_sdk_upload_lock = None
_sdk_upload_lock_loop = None


def _get_sdk_upload_lock():
    import asyncio

    global _sdk_upload_lock, _sdk_upload_lock_loop
    loop = asyncio.get_running_loop()
    if _sdk_upload_lock is None or _sdk_upload_lock_loop is not loop:
        _sdk_upload_lock = asyncio.Lock()
        _sdk_upload_lock_loop = loop
    return _sdk_upload_lock


def _get_observability_url() -> str | None:
    return os.environ.get("AGENT_OBSERVABILITY_URL")


def merge_tagger_tags_into_dict(
    d: Any,
    tagger_tags: Any,
) -> Any:
    """Merge tagger.tags into the chat_history dict's `tags[]` field.

    The obs server's extractAgentId reads `agent_id:<value>` from
    rawReport.tags[] (the multipart's chat_history JSON). LiveKit's
    ``ChatContext.to_dict`` doesn't include tagger tags by default — they
    only ride along on the OTLP "tag" body, which arrives *after* the
    multipart. Without this merge, the recording multipart hits the obs
    server first, INSERT runs with agent_id=null, the NOT NULL column
    rejects the row, and the OTLP tag has no row to UPDATE.

    Exported as a pure helper so tests can pin the contract without
    setting up the full LiveKit upload pipeline.
    """
    if not isinstance(d, dict):
        return d
    tags = sorted(tagger_tags or [])
    if not tags:
        return d
    existing = d.get("tags")
    if isinstance(existing, list):
        # Preserve any tags LiveKit may have already serialized (none
        # today, but stay defensive about upstream changes).
        seen = set(existing)
        for t in tags:
            if t not in seen:
                existing.append(t)
    else:
        d["tags"] = list(tags)
    return d


def _ensure_transport_tags(
    tagger: Any,
    *,
    account_id: str | None,
    transport: str | None,
    direction: str | None,
    agent_id: str,
    agent_name: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    add = getattr(tagger, "add", None)
    if not callable(add):
        return

    session_metadata = {
        **(metadata or {}),
        "agent_id": agent_id,
        "agent_name": agent_name,
        **({"account_id": account_id} if account_id else {}),
        **({"transport": transport} if transport else {}),
        **({"direction": direction} if direction else {}),
    }

    add("agent.session", metadata=session_metadata)
    # agent_id:<value> matches the obs server's applySessionTagMetadata
    # prefix extractor — landing the UUID into agent_transport_sessions
    # .agent_id directly. agent_name preserved for human-readable display.
    add(f"agent_id:{agent_id}", metadata={"agent_id": agent_id})
    add(f"agent_name:{agent_name}", metadata={"agent_name": agent_name})
    if account_id:
        add(f"account_id:{account_id}", metadata={"account_id": account_id})
    if transport:
        add(f"transport:{transport}", metadata={"transport": transport})
    if direction:
        add(f"direction:{direction}", metadata={"direction": direction})
    # Escape hatch: caller can flag a session as evaluation-capable from app
    # code without registering a JudgeGroup (e.g., custom eval pipeline).
    if metadata and metadata.get("evaluations"):
        add("evaluations:enabled", metadata=None)


def _get_sdk_tagger() -> Any:
    from livekit.agents.observability import Tagger

    return Tagger()


def _emit_runtime_events(session: Any, report: Any) -> None:
    """Ship the runtime event log the SDK leaves out of the wire format.

    Mirrors the Node SDK's ``sessionReportToJSON`` behavior, which embeds
    ``report.events`` (state changes, transcribed input, function-tool
    execution, etc.) inside the session-report record's payload. The
    Python SDK's ``_upload_session_report`` deliberately omits
    ``session._recorded_events`` from the wire format — so we emit one
    extra OTLP record with ``body="session report"`` and the missing
    events nested under ``attributes["session.report"]``. The obs
    server's existing session-report branch spreads that payload into
    ``raw_report``, picking up the events alongside the SDK's records
    with no new ingest code.

    Filter mirrors Node's:
      - skip ``metrics_collected`` / ``session_usage_updated`` (covered
        by the SDK's ``usage`` attribute)
      - additionally skip ``conversation_item_added`` for Python only
        (the SDK already emits one ``"chat item"`` record per item;
        re-shipping them here would duplicate every message in the
        dashboard's events list).
    """
    import time

    try:
        from opentelemetry._logs import SeverityNumber, get_logger_provider
    except Exception:
        return

    events = getattr(session, "_recorded_events", None) or []
    if not events:
        return

    serialized: list[dict[str, Any]] = []
    for ev in events:
        ev_type = getattr(ev, "type", None)
        if not ev_type:
            continue
        if ev_type in ("metrics_collected", "session_usage_updated", "conversation_item_added"):
            continue
        try:
            payload = ev.model_dump(mode="json")
        except Exception:
            payload = {"type": ev_type}
        serialized.append(payload)

    if not serialized:
        return

    rt_logger = get_logger_provider().get_logger(
        name="chat_history",
        attributes={
            "room_id": report.room_id,
            "job_id": report.job_id,
            "room": report.room,
        },
    )

    try:
        rt_logger.emit(
            body="session report",
            timestamp=int((report.started_at or report.timestamp or time.time()) * 1e9),
            attributes={"session.report": {"events": serialized}},
            severity_number=SeverityNumber.UNSPECIFIED,
            severity_text="unspecified",
        )
    except Exception:
        logger.warning(
            "Failed to emit runtime events session-report record (%d events)",
            len(serialized),
            exc_info=True,
        )


async def upload_session_report(
    session: Any,
    session_id: str,
    obs_url: str,
    agent_name: str,
    recording_path: str | None = None,
    recording_started_at: float | None = None,
    account_id: str | None = None,
    agent_id: str = "",
    transport: str | None = None,
    direction: str | None = None,
    metadata: dict[str, Any] | None = None,
    tagger: Any = None,
    job_context: Any = None,
) -> None:
    """Upload the session through LiveKit's native SDK telemetry helpers."""
    if not os.environ.get("LIVEKIT_API_KEY") or not os.environ.get("LIVEKIT_API_SECRET"):
        raise RuntimeError(
            "LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required for native "
            "LiveKit observability uploads"
        )

    import aiohttp
    from livekit.agents.telemetry.traces import (
        _setup_cloud_tracer,
        _shutdown_telemetry,
        _upload_session_report,
    )

    tagger = tagger or getattr(job_context, "tagger", None) or _get_sdk_tagger()
    _ensure_transport_tags(
        tagger,
        account_id=account_id,
        transport=transport,
        direction=direction,
        agent_id=agent_id,
        agent_name=agent_name,
        metadata=metadata,
    )

    make_report = getattr(job_context, "make_session_report", None)
    if not callable(make_report):
        raise RuntimeError("job_context with make_session_report is required for native upload")

    report = make_report(
        session,
        recording_path=recording_path,
        recording_started_at=recording_started_at,
        recording_options={
            "audio": bool(recording_path and os.path.exists(recording_path)),
            "traces": False,
            "logs": True,
            "transcript": True,
        },
    )

    logger.info("Uploading native LiveKit session report for %s to %s", session_id, obs_url)
    async with _get_sdk_upload_lock():
        _setup_cloud_tracer(
            room_id=report.room_id,
            job_id=report.job_id,
            observability_url=obs_url,
            enable_traces=False,
            enable_logs=True,
        )
        # The multipart that _upload_session_report sends arrives at obs
        # before the OTLP records (multipart is sync; OTLP is buffered).
        # LiveKit doesn't put the tagger's tags in the multipart's
        # chat_history JSON — they're only in the OTLP "session report"
        # body's `session.tags` attr — but obs's recording validator runs
        # at multipart time and needs to extract `agent_id` from
        # `rawReport.tags[]` (the `agent_id:<value>` prefix tag that
        # _ensure_transport_tags already added to the tagger).
        #
        # Patch `report.chat_history.to_dict` to mirror tagger.tags into
        # the multipart, riding alongside the chat items. obs's existing
        # `extractAgentId(rawReport)` picks it up; the other prefix tags
        # (account_id:, transport:, …) carry through too. agent_name
        # arrives shortly after via the OTLP "tag" body, which obs
        # persists and runs `applySessionTagMetadata` on (UPDATE the
        # session + upsertAgent merge).
        chat_ctx = report.chat_history
        original_to_dict = chat_ctx.to_dict

        def _patched_to_dict(*args, **kwargs):
            d = original_to_dict(*args, **kwargs)
            return merge_tagger_tags_into_dict(d, getattr(tagger, "tags", None))

        chat_ctx.to_dict = _patched_to_dict  # type: ignore[assignment]

        try:
            async with aiohttp.ClientSession() as http_session:
                await _upload_session_report(
                    agent_name=agent_name,
                    observability_url=obs_url,
                    report=report,
                    tagger=tagger,
                    http_session=http_session,
                )
                # Emit runtime events into the same OTLP pipeline so they
                # ride to obs alongside the SDK's records. _shutdown_telemetry
                # below flushes everything out.
                _emit_runtime_events(session, report)
        finally:
            # Restore the original to_dict so we don't bleed into any
            # subsequent re-serialization of this report.
            try:
                chat_ctx.to_dict = original_to_dict  # type: ignore[assignment]
            except Exception:
                pass
            _shutdown_telemetry()

    logger.info("Native LiveKit session report uploaded for %s", session_id)
