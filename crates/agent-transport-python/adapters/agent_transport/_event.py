"""LiveKit-shape FfiEvent classes.

Pure-Python mirrors of the relevant subset of ``livekit/rtc/_proto/ffi_pb2.py``,
giving us the same attribute-access surface as LiveKit's protobuf-generated
event objects without taking a protobuf dependency.

Consumers can use exactly LiveKit's predicate style:

    e.WhichOneof("message") == "capture_audio_frame"
    e.capture_audio_frame.async_id == resp_async_id

So that downstream code can be (eventually) line-for-line identical to
``livekit/rtc/audio_source.py``, ``audio_stream.py``, ``participant.py`` etc.

For events that have no LiveKit equivalent (Plivo ``playedStream`` checkpoint
confirmation, beep detection, SIP register/unregister), we add a parallel
``transport_event`` oneof field. Keeps the access pattern uniform while
clearly demarcating our extensions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ─── Sub-payloads (mirror LiveKit's nested message types) ────────────────────


@dataclass(slots=True)
class CaptureAudioFrameCallback:
    """Mirror of ``audio_frame_pb2.CaptureAudioFrameCallback``.

    Used for both immediate-success and error replies to a
    ``capture_audio_frame`` request. An empty ``error`` string indicates
    success; a non-empty string indicates failure (matches LiveKit's
    contract — see ``livekit/rtc/audio_source.py:148-149``).

    ``source_handle`` is our extension: LiveKit identifies audio sources
    by their FFI handle (a ``u64``); we identify them by our session_id
    string. Consumers filter by ``source_handle == self._id`` to scope
    the event to their own audio source.

    ``cancelled`` is our extension (no LiveKit equivalent): ``True``
    when this completion was synthesized by ``clear_buffer()`` (the
    pending capture was discarded silently rather than transmitted).
    LiveKit consumers ignore this — clear semantics for them are
    silent-discard, matching their ``rtc.AudioSource.clear_queue``
    behaviour. Pipecat's ``write_audio_frame -> bool`` API checks it
    so a cleared frame counts as dropped, not delivered.
    """
    async_id: int = 0
    error: str = ""
    source_handle: str = ""
    cancelled: bool = False


@dataclass(slots=True)
class AudioFrameReceived:
    """Mirror of ``audio_frame_pb2.AudioFrameReceived``."""
    frame: Any = None  # OwnedAudioFrameBuffer — we use bytes for simplicity


@dataclass(slots=True)
class AudioStreamEOS:
    """Mirror of ``audio_frame_pb2.AudioStreamEOS``."""
    pass


@dataclass(slots=True)
class AudioStreamEvent:
    """Mirror of ``audio_frame_pb2.AudioStreamEvent``.

    Used for inbound audio (STT input path). Consumers filter by
    ``stream_handle`` then ``HasField("frame_received")`` /
    ``HasField("eos")`` (we expose ``which_event`` as a string for
    convenience too).
    """
    stream_handle: str = ""
    frame_received: Optional[AudioFrameReceived] = None
    eos: Optional[AudioStreamEOS] = None

    def HasField(self, name: str) -> bool:
        return getattr(self, name, None) is not None


# ─── Room sub-events ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class ParticipantInfo:
    """Subset of ``room_pb2.ParticipantInfo`` used by the agent stack.

    The trailing fields (``session_id``, ``stream_id``, ``extra_headers``)
    are our extensions — LiveKit's wire ParticipantInfo doesn't have them,
    but our lifecycle loop needs to thread Plivo's stream_id and SIP custom
    headers from the Rust ``CallSession`` payload to the server's
    ``_start_session``/``_start_call`` paths. They're additive — code that
    only cares about LiveKit-shape fields just ignores them.
    """
    identity: str = ""
    name: str = ""
    kind: int = 0  # rtc.ParticipantKind enum — 0 STANDARD, 4 SIP
    session_id: str = ""
    stream_id: str = ""
    extra_headers: dict = field(default_factory=dict)


@dataclass(slots=True)
class ParticipantConnected:
    info: Optional[ParticipantInfo] = None


@dataclass(slots=True)
class ParticipantDisconnected:
    participant_identity: str = ""
    disconnect_reason: int = 0  # 1 = CLIENT_INITIATED (we use this)
    # Our extension — carries the session_id and the human reason string
    # from Rust ``CallTerminated`` (e.g., "bye", "buffer_dropped").
    session_id: str = ""
    reason: str = ""


@dataclass(slots=True)
class SipDTMF:
    """Mirror of ``room_pb2.SipDTMF``."""
    code: int = 0
    digit: str = ""


@dataclass(slots=True)
class DataPacket:
    """Subset of ``room_pb2.DataPacket`` — currently used only for DTMF."""
    sip_dtmf: Optional[SipDTMF] = None
    participant_identity: str = ""


@dataclass(slots=True)
class DataPacketReceived:
    value: Optional[DataPacket] = None


@dataclass(slots=True)
class Disconnected:
    reason: int = 0


@dataclass(slots=True)
class RoomEOS:
    pass


_ROOM_EVENT_VARIANTS = (
    "participant_connected",
    "participant_disconnected",
    "data_packet_received",
    "disconnected",
    "eos",
)


@dataclass(slots=True)
class RoomEvent:
    """Mirror of ``room_pb2.RoomEvent``. Carries one of N sub-events.

    ``WhichOneof`` returns the name of the populated sub-event, matching
    protobuf's ``oneof`` access pattern.
    """
    room_handle: str = ""
    participant_connected: Optional[ParticipantConnected] = None
    participant_disconnected: Optional[ParticipantDisconnected] = None
    data_packet_received: Optional[DataPacketReceived] = None
    disconnected: Optional[Disconnected] = None
    eos: Optional[RoomEOS] = None

    def WhichOneof(self, _name: str) -> Optional[str]:
        for v in _ROOM_EVENT_VARIANTS:
            if getattr(self, v, None) is not None:
                return v
        return None


# ─── Our extension: transport-specific events ────────────────────────────────


@dataclass(slots=True)
class PlayoutCompleteCallback:
    """No LiveKit equivalent — LiveKit estimates playout in Python; we get
    real Rust-buffer-empty + (for audio_stream) Plivo ``playedStream``
    confirmation. Same async_id pattern as ``CaptureAudioFrameCallback``."""
    async_id: int = 0
    error: str = ""
    source_handle: str = ""


@dataclass(slots=True)
class BeepDetected:
    source_handle: str = ""
    frequency_hz: float = 0.0
    duration_ms: int = 0


@dataclass(slots=True)
class BeepTimeout:
    source_handle: str = ""


@dataclass(slots=True)
class EndpointRegistered:
    pass


@dataclass(slots=True)
class EndpointUnregistered:
    pass


@dataclass(slots=True)
class EndpointShutdown:
    pass


@dataclass(slots=True)
class CallRinging:
    """SIP pre-answer hook. No LiveKit equivalent — we surface it so
    server-level ``on("ringing")`` listeners can screen calls before
    answer."""
    session_id: str = ""
    remote_uri: str = ""
    call_uuid: str = ""


_TRANSPORT_EVENT_VARIANTS = (
    "playout_complete",
    "beep_detected",
    "beep_timeout",
    "endpoint_registered",
    "endpoint_unregistered",
    "endpoint_shutdown",
    "call_ringing",
)


@dataclass(slots=True)
class TransportEvent:
    """Our extension oneof: carries events that don't map to any LiveKit
    FfiEvent variant. Same nested access pattern so consumers don't
    need a separate dispatch idiom."""
    playout_complete: Optional[PlayoutCompleteCallback] = None
    beep_detected: Optional[BeepDetected] = None
    beep_timeout: Optional[BeepTimeout] = None
    endpoint_registered: Optional[EndpointRegistered] = None
    endpoint_unregistered: Optional[EndpointUnregistered] = None
    endpoint_shutdown: Optional[EndpointShutdown] = None
    call_ringing: Optional[CallRinging] = None

    def WhichOneof(self, _name: str) -> Optional[str]:
        for v in _TRANSPORT_EVENT_VARIANTS:
            if getattr(self, v, None) is not None:
                return v
        return None


# ─── Top-level FfiEvent ──────────────────────────────────────────────────────


_FFI_EVENT_VARIANTS = (
    "capture_audio_frame",
    "audio_stream_event",
    "room_event",
    "transport_event",
)


@dataclass(slots=True)
class FfiEvent:
    """Top-level event container — mirror of ``ffi_pb2.FfiEvent`` for the
    subset of variants we emit.

    ``WhichOneof("message")`` returns the populated oneof field name (or
    ``None`` if empty), matching LiveKit's protobuf API exactly.
    Consumer predicates look like:

        queue = GLOBAL.subscribe(
            filter_fn=lambda e: e.WhichOneof("message") == "capture_audio_frame",
        )
        cb = await queue.wait_for(
            lambda e: e.capture_audio_frame.async_id == async_id
                      and e.capture_audio_frame.source_handle == self._id,
        )
        if cb.capture_audio_frame.error:
            raise RuntimeError(cb.capture_audio_frame.error)
    """
    capture_audio_frame: Optional[CaptureAudioFrameCallback] = None
    audio_stream_event: Optional[AudioStreamEvent] = None
    room_event: Optional[RoomEvent] = None
    transport_event: Optional[TransportEvent] = None

    def WhichOneof(self, name: str) -> Optional[str]:
        if name != "message":  # protobuf's oneof name is "message"
            return None
        for v in _FFI_EVENT_VARIANTS:
            if getattr(self, v, None) is not None:
                return v
        return None
