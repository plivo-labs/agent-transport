"""Constrained event sink — the body invoked on the Rust dispatcher thread.

Architecture: the pyo3 ``set_event_sink`` registers a Python callable that
the Rust dispatcher thread invokes (with the GIL acquired) once per event
pulled off ``inner.events()``. The sink translates our Rust ``EndpointEvent``
into a LiveKit-shape :class:`FfiEvent` and puts it into the process-global
:data:`agent_transport._ffi_queue.GLOBAL` broker. From there subscribers
(audio sources, lifecycle listeners) consume on the asyncio loop via
``call_soon_threadsafe``.

**Contract for ``_on_event_from_rust`` (the sink body):**

* Runs on the Rust dispatcher thread with the GIL held.
* MUST be non-blocking, non-locking, and MUST NOT re-enter Rust.
* MUST NOT raise. Errors are swallowed by the dispatcher (logged but the
  loop continues).

This mirrors LiveKit's ``ffi_event_callback`` (``livekit/rtc/_ffi_client.py:152``)
which runs on the WebRTC FFI's C-callback thread and does exactly the same
shape of work: parse, route into queue, return.

The constraint that prevents the AB-BA deadlock we fixed in Phase A is that
this sink does not acquire any Rust mutex (it only touches our pure-Python
``FfiQueue``, whose ``put`` does ``loop.call_soon_threadsafe`` which only
briefly acquires asyncio-internal locks). So even though the dispatcher
thread holds the GIL during this call, the call cannot deadlock with any
Rust thread that is also waiting on the GIL — because the sink never asks
for a Rust mutex.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ._event import (
    AudioFrameReceived,
    AudioStreamEOS,
    AudioStreamEvent,
    BeepDetected,
    BeepTimeout,
    CallRinging,
    CaptureAudioFrameCallback,
    DataPacket,
    DataPacketReceived,
    Disconnected,
    EndpointRegistered,
    EndpointShutdown,
    EndpointUnregistered,
    FfiEvent,
    ParticipantConnected,
    ParticipantDisconnected,
    ParticipantInfo,
    PlayoutCompleteCallback,
    RoomEOS,
    RoomEvent,
    SipDTMF,
    TransportEvent,
)
from ._ffi_queue import GLOBAL, GLOBAL_DICT

logger = logging.getLogger(__name__)


_SINK_COUNTERS: dict = {}

def _on_event_from_rust(event_dict: Mapping[str, Any]) -> None:
    """Sink body. Invoked by the Rust dispatcher thread, once per event.

    See module docstring for the contract.
    """
    try:
        events = _build_ffi_events(event_dict)
    except Exception:
        logger.exception("event sink: failed to build FfiEvent from %r", event_dict)
        return
    # Throttled debug logging — every 50th event of each type and every
    # lifecycle/error event. Useful to confirm the dispatcher thread is
    # firing and what it's translating.
    ev_type = event_dict.get("type", "?")
    _SINK_COUNTERS[ev_type] = _SINK_COUNTERS.get(ev_type, 0) + 1
    count = _SINK_COUNTERS[ev_type]
    log_this = (
        count == 1
        or ev_type not in ("audio_capture_complete", "audio_playout_complete")
        or count % 50 == 0
    )
    if log_this:
        logger.debug(
            "event_sink: type=%s count=%d → %d FfiEvent(s)",
            ev_type, count, len(events),
        )
    # Routing key for the FfiQueue: per-frame async-id completion events carry
    # ``session_id``, so keying lets put() dispatch only to that session's
    # subscriber instead of scanning every concurrent call's subscriber.
    # Lifecycle events that only carry a ``session`` object have no top-level
    # session_id → key is None → broadcast (correct, and they're low-frequency).
    _sid = event_dict.get("session_id")
    # Match the exact form the subscribers key on: source_handle is built as
    # ``str(session_id)`` (and audio_source subscribes with key=self._id, a str),
    # so wrap here too. Absent session_id → None → broadcast.
    key = str(_sid) if _sid is not None else None
    for ev in events:
        try:
            GLOBAL.put(ev, key=key)
        except Exception:
            logger.exception("event sink: GLOBAL.put failed")
    # Parallel fan-out to the dict-shaped broker for pipecat-style
    # consumers. Same event payload, no translation — pipecat reads
    # ``event["session"].session_id`` and similar attribute-on-PyO3
    # accesses that LiveKit-shape FfiEvent would have flattened.
    try:
        GLOBAL_DICT.put(dict(event_dict), key=key)
    except Exception:
        logger.exception("event sink: GLOBAL_DICT.put failed")


def _build_ffi_events(d: Mapping[str, Any]):
    """Translate a Rust event dict to one or more FfiEvents.

    Most events map 1-to-1; ``audio_capture_error`` is the one
    exception. Rust emits the same ``AudioCaptureError`` variant for any
    pending async_id when the buffer is cleared / dropped — capture or
    playout. We can't tell from the dict which kind the awaiter is, so we
    fan to BOTH ``capture_audio_frame`` and
    ``transport_event.playout_complete`` variants. Whichever awaiter is
    waiting on that async_id matches and raises; the other variant is
    discarded by the broker's filter (subscriber count is still 0 for
    that variant if there's no in-flight op).
    """
    ev_type = d.get("type", "")
    if ev_type == "audio_capture_error":
        async_id = int(d.get("async_id", 0))
        error = str(d.get("error", "unknown"))
        sid = str(d.get("session_id", ""))
        return (
            FfiEvent(
                capture_audio_frame=CaptureAudioFrameCallback(
                    async_id=async_id, error=error, source_handle=sid,
                )
            ),
            FfiEvent(
                transport_event=TransportEvent(
                    playout_complete=PlayoutCompleteCallback(
                        async_id=async_id, error=error, source_handle=sid,
                    )
                )
            ),
        )
    ev = _build_ffi_event(d)
    return (ev,) if ev is not None else ()


# ─── Rust-dict → LiveKit-shape FfiEvent translation ──────────────────────────


def _participant_kind_for_transport(transport_hint: str) -> int:
    """Map our transport hint to rtc.ParticipantKind enum.

    rtc.ParticipantKind:  STANDARD=0, INGRESS=1, EGRESS=2, AGENT=3, SIP=4
    Audio_stream calls come via Plivo's WebSocket bridge — caller is a PSTN
    line, so SIP (4) is the most semantically accurate; SIP transport
    is naturally SIP=4 too.
    """
    return 4  # SIP for both transports — both originate from PSTN/SIP networks


def _build_ffi_event(d: Mapping[str, Any]) -> FfiEvent | None:
    """Translate a Rust ``event_to_dict`` payload into a LiveKit-shape FfiEvent.

    Returns ``None`` for event types we deliberately don't surface (none today
    — included as a future safety hatch).
    """
    ev_type = d.get("type", "")

    # ─── Audio backpressure events → capture_audio_frame ─────────────────────
    if ev_type == "audio_capture_complete":
        return FfiEvent(
            capture_audio_frame=CaptureAudioFrameCallback(
                async_id=int(d.get("async_id", 0)),
                error="",
                source_handle=str(d.get("session_id", "")),
                cancelled=bool(d.get("cancelled", False)),
            )
        )
    if ev_type == "audio_capture_error":
        return FfiEvent(
            capture_audio_frame=CaptureAudioFrameCallback(
                async_id=int(d.get("async_id", 0)),
                error=str(d.get("error", "unknown")),
                source_handle=str(d.get("session_id", "")),
            )
        )
    if ev_type == "audio_buffer_drained":
        # Treat as a non-error capture completion. (We don't currently emit
        # this from the Rust core — kept for completeness.)
        return FfiEvent(
            capture_audio_frame=CaptureAudioFrameCallback(
                async_id=int(d.get("async_id", 0)),
                error="",
                source_handle=str(d.get("session_id", "")),
            )
        )

    # ─── Playout (our extension; no LiveKit equivalent) ──────────────────────
    if ev_type == "audio_playout_complete":
        return FfiEvent(
            transport_event=TransportEvent(
                playout_complete=PlayoutCompleteCallback(
                    async_id=int(d.get("async_id", 0)),
                    error="",
                    source_handle=str(d.get("session_id", "")),
                )
            )
        )

    # ─── Call lifecycle → room_event ─────────────────────────────────────────
    if ev_type == "call_answered":
        session = d.get("session")
        # CallSession is a pyo3 class; access via attributes.
        sid = getattr(session, "session_id", "") if session else d.get("session_id", "")
        remote = getattr(session, "remote_uri", "") if session else ""
        stream_id = getattr(session, "local_uri", "") if session else ""
        try:
            extra_headers = dict(getattr(session, "extra_headers", {}) or {}) if session else {}
        except Exception:
            extra_headers = {}
        return FfiEvent(
            room_event=RoomEvent(
                room_handle=str(sid),
                participant_connected=ParticipantConnected(
                    info=ParticipantInfo(
                        identity=str(remote),
                        name=str(remote),
                        kind=_participant_kind_for_transport(""),
                        session_id=str(sid),
                        stream_id=str(stream_id),
                        extra_headers=extra_headers,
                    )
                ),
            )
        )

    if ev_type == "call_terminated":
        session = d.get("session")
        sid = getattr(session, "session_id", "") if session else d.get("session_id", "")
        remote = getattr(session, "remote_uri", "") if session else ""
        reason = str(d.get("reason", "unknown"))
        return FfiEvent(
            room_event=RoomEvent(
                room_handle=str(sid),
                participant_disconnected=ParticipantDisconnected(
                    participant_identity=str(remote),
                    disconnect_reason=1,  # CLIENT_INITIATED
                    session_id=str(sid),
                    reason=reason,
                ),
            )
        )

    if ev_type == "call_ringing":
        session = d.get("session")
        sid = getattr(session, "session_id", "") if session else d.get("session_id", "")
        remote = getattr(session, "remote_uri", "") if session else ""
        call_uuid = getattr(session, "call_uuid", "") if session else ""
        return FfiEvent(
            transport_event=TransportEvent(
                call_ringing=CallRinging(
                    session_id=str(sid),
                    remote_uri=str(remote),
                    call_uuid=str(call_uuid),
                )
            )
        )

    if ev_type == "dtmf_received":
        digit = str(d.get("digit", ""))
        return FfiEvent(
            room_event=RoomEvent(
                room_handle=str(d.get("session_id", "")),
                data_packet_received=DataPacketReceived(
                    value=DataPacket(
                        sip_dtmf=SipDTMF(
                            code=ord(digit) if digit else 0,
                            digit=digit,
                        ),
                        participant_identity="",
                    )
                ),
            )
        )

    # ─── Beep / shutdown / SIP register — transport_event extension ──────────
    if ev_type == "beep_detected":
        return FfiEvent(
            transport_event=TransportEvent(
                beep_detected=BeepDetected(
                    source_handle=str(d.get("session_id", "")),
                    frequency_hz=float(d.get("frequency_hz", 0.0)),
                    duration_ms=int(d.get("duration_ms", 0)),
                )
            )
        )

    if ev_type == "beep_timeout":
        return FfiEvent(
            transport_event=TransportEvent(
                beep_timeout=BeepTimeout(
                    source_handle=str(d.get("session_id", "")),
                )
            )
        )

    if ev_type == "registered":
        return FfiEvent(
            transport_event=TransportEvent(endpoint_registered=EndpointRegistered())
        )

    if ev_type == "unregistered":
        return FfiEvent(
            transport_event=TransportEvent(endpoint_unregistered=EndpointUnregistered())
        )

    if ev_type == "shutdown":
        return FfiEvent(
            transport_event=TransportEvent(endpoint_shutdown=EndpointShutdown())
        )

    # Pre-answer SIP-only events (CallRinging, CallStateChanged) and a few
    # others — surface them as room_event with synthetic shape, but we
    # don't currently consume them in adapters, so it's safe to drop.
    logger.debug("event sink: unmapped event type %r — dropping", ev_type)
    return None
