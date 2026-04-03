"""TransportRoom — facade implementing rtc.Room interface for SIP/AudioStream transport.

Makes get_job_context().room work so existing LiveKit agent code (GetDtmfTask,
SendDtmfTool, background audio, transcription, warm transfer) runs unchanged.

Architecture:
- TransportRoom extends rtc.EventEmitter (same base as rtc.Room)
- _TransportLocalParticipant maps publish_dtmf → ep.send_dtmf, etc.
- _StubJobContext provides .room, .job for AgentSession's get_job_context() calls
- Server event loop routes DTMF events → room.emit("sip_dtmf_received", SipDTMF(...))
"""

import asyncio
import datetime
import logging
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine

from livekit import rtc
from livekit.rtc.event_emitter import EventEmitter
from livekit.rtc.room import SipDTMF

logger = logging.getLogger(__name__)


# ─── Stub track publication (returned by publish_track) ──────────────────────

class _StubTrackPublication:
    def __init__(self, track=None, sid=None):
        self.track = track
        self.sid = sid or f"TR_{uuid.uuid4().hex[:8]}"
        self.name = ""
        self.kind = 0  # AUDIO
        self.source = 1  # MICROPHONE
        self.muted = False
        self.simulcasted = False
        self.width = 0
        self.height = 0
        self.mime_type = "audio/opus"
        self.encryption_type = 0
        self.audio_features = []

    async def wait_for_subscription(self):
        pass  # No remote subscribers in our transport


# ─── Stub text stream writer (returned by stream_text) ──────────────────────

class _StubTextStreamWriter:
    async def write(self, text: str):
        pass

    async def aclose(self, **kwargs):
        pass


# ─── Transport Local Participant ─────────────────────────────────────────────

class _TransportLocalParticipant:
    """Facade for rtc.LocalParticipant — maps to our endpoint."""

    def __init__(self, endpoint, session_id, agent_name):
        self._ep = endpoint
        self._sid = session_id
        # Participant properties
        self.sid = f"PA_{session_id}"
        self.identity = agent_name
        self.name = agent_name
        self.metadata = ""
        self.attributes: dict[str, str] = {}
        self.kind = 0  # STANDARD
        self.permissions = None
        self.disconnect_reason = None
        self.track_publications: dict[str, _StubTrackPublication] = {}
        self._track_forward_tasks: dict[str, asyncio.Task] = {}

    # ─── Real implementations (mapped to endpoint) ───────────────────────

    async def publish_dtmf(self, *, code: int, digit: str) -> None:
        """Send DTMF — maps to ep.send_dtmf()."""
        self._ep.send_dtmf(self._sid, digit)

    async def publish_track(self, track, options=None):
        """Publish a track — reads audio from it and mixes into our transport.

        For audio tracks (e.g., BackgroundAudioPlayer), creates an AudioStream
        to read frames from the track and forwards them to the Rust endpoint's
        background mixer. This is transparent — same API as LiveKit WebRTC.
        """
        pub = _StubTrackPublication(track)
        self.track_publications[pub.sid] = pub

        # For audio tracks, start forwarding frames to our endpoint's mixer
        if track is not None and isinstance(track, rtc.LocalAudioTrack):
            task = asyncio.create_task(
                self._forward_track_audio(pub.sid, track))
            self._track_forward_tasks[pub.sid] = task

        return pub

    async def unpublish_track(self, track_sid: str) -> None:
        """Stop publishing a track — cancels the forwarding task."""
        self.track_publications.pop(track_sid, None)
        task = self._track_forward_tasks.pop(track_sid, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _forward_track_audio(self, pub_sid: str, track: rtc.LocalAudioTrack) -> None:
        """Read frames from a published audio track and send to endpoint's background mixer.

        Creates an rtc.AudioStream from the local track (loopback read),
        resamples to endpoint's pipeline rate, and forwards to ep.send_background_audio().
        Rust send loop mixes this with agent voice before encoding.
        """
        try:
            sr = self._ep.input_sample_rate if self._ep is not None else 8000
            stream = rtc.AudioStream.from_track(
                track=track, sample_rate=sr, num_channels=1)
            logger.debug("Forwarding published track %s to background mixer", pub_sid)
            frame_count = 0

            async for event in stream:
                frame = event.frame
                frame_count += 1
                if frame_count == 1:
                    logger.info("Background audio: first frame sr=%d samples=%d", frame.sample_rate, frame.samples_per_channel)
                elif frame_count % 500 == 0:
                    logger.info("Background audio: %d frames forwarded", frame_count)
                if frame.samples_per_channel > 0 and self._ep is not None:
                    try:
                        self._ep.send_background_audio(
                            self._sid,
                            bytes(frame.data),
                            frame.sample_rate,
                            frame.num_channels,
                        )
                    except Exception as e:
                        logger.error("Background audio send failed: %s", e)
                        break  # Session gone

            await stream.aclose()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Track forwarding ended for %s", pub_sid)

    async def publish_transcription(self, transcription) -> None:
        pass  # Transcription goes through AudioOutput text chain

    async def stream_text(self, *, destination_identities=None, topic="",
                          attributes=None, stream_id=None, reply_to_id=None,
                          total_size=None, sender_identity=None, **kw):
        return _StubTextStreamWriter()

    async def send_text(self, text, *, destination_identities=None, topic="",
                        attributes=None, reply_to_id=None):
        pass

    async def publish_data(self, payload, *, reliable=True,
                           destination_identities=None, topic="", **kw) -> None:
        if isinstance(payload, (str, bytes)):
            try:
                msg = payload if isinstance(payload, str) else payload.decode()
                self._ep.send_raw_message(self._sid, msg)
            except Exception:
                pass

    async def set_metadata(self, metadata: str) -> None:
        self.metadata = metadata

    async def set_name(self, name: str) -> None:
        self.name = name

    async def set_attributes(self, attributes: dict[str, str]) -> None:
        self.attributes.update(attributes)

    def register_rpc_method(self, method_name, handler=None):
        if handler is not None:
            return handler
        return lambda fn: fn

    def unregister_rpc_method(self, method):
        pass

    def set_track_subscription_permissions(self, *, allow_all_participants=True,
                                           participant_permissions=None):
        pass

    async def perform_rpc(self, *, destination_identity, method, payload,
                          response_timeout=None):
        return ""

    async def send_file(self, file_path, **kw):
        pass

    async def stream_bytes(self, name, **kw):
        return _StubTextStreamWriter()  # Close enough interface


# ─── Transport Remote Participant ────────────────────────────────────────────

class _TransportRemoteParticipant:
    """Stub for the remote caller."""

    def __init__(self, identity, session_id):
        self.sid = f"PR_{session_id}"
        self.identity = identity
        self.name = identity
        self.metadata = ""
        self.attributes: dict[str, str] = {}
        self.kind = 3  # PARTICIPANT_KIND_SIP (rtc.ParticipantKind.PARTICIPANT_KIND_SIP = 3)
        self.permissions = None
        self.disconnect_reason = None
        self.track_publications: dict = {}


# ─── Transport Room ──────────────────────────────────────────────────────────

class TransportRoom(EventEmitter):
    """Facade Room wrapping SipEndpoint/AudioStreamEndpoint.

    Extends rtc.EventEmitter so on()/off()/emit() work exactly like rtc.Room.
    LiveKit agents code that does room.on("sip_dtmf_received", handler) works unchanged.
    """

    def __init__(self, endpoint, session_id, *, agent_name, caller_identity):
        super().__init__()
        self._ep = endpoint
        self._sid = session_id
        self._connected = True

        self._local_participant = _TransportLocalParticipant(
            endpoint, session_id, agent_name)
        self._remote = _TransportRemoteParticipant(caller_identity, str(session_id))
        self._remote_participants = {caller_identity: self._remote}
        self._name = f"transport-{session_id}"
        self._creation_time = datetime.datetime.now(datetime.timezone.utc)
        self._text_stream_handlers: dict[str, Any] = {}
        self._byte_stream_handlers: dict[str, Any] = {}
        self._token: str | None = None
        self._server_url: str | None = None

    # ─── Properties (match rtc.Room) ─────────────────────────────────────

    @property
    def local_participant(self):
        return self._local_participant

    @property
    def remote_participants(self):
        return self._remote_participants

    @property
    def name(self) -> str:
        return self._name

    @property
    def sid(self) -> str:
        return self._name

    @property
    def metadata(self) -> str:
        return ""

    @property
    def connection_state(self):
        return 3 if self._connected else 5  # CONNECTED / DISCONNECTED

    @property
    def num_participants(self) -> int:
        return len(self._remote_participants)

    @property
    def num_publishers(self) -> int:
        return 0

    @property
    def is_recording(self) -> bool:
        return False

    @property
    def departure_timeout(self) -> float:
        return 0.0

    @property
    def empty_timeout(self) -> float:
        return 0.0

    @property
    def e2ee_manager(self):
        return None

    @property
    def creation_time(self) -> datetime.datetime:
        return self._creation_time

    # ─── Methods ─────────────────────────────────────────────────────────

    def isconnected(self) -> bool:
        return self._connected

    async def connect(self, url="", token="", options=None):
        logger.debug("TransportRoom.connect() — already connected via transport (no WebRTC room)")
        self._token = token
        self._server_url = url

    async def disconnect(self):
        self._connected = False
        self.emit("disconnected")

    async def get_rtc_stats(self):
        return None

    # ─── Stream handlers ─────────────────────────────────────────────────

    def register_text_stream_handler(self, topic, handler):
        self._text_stream_handlers[topic] = handler

    def unregister_text_stream_handler(self, topic):
        self._text_stream_handlers.pop(topic, None)

    def register_byte_stream_handler(self, topic, handler):
        self._byte_stream_handlers[topic] = handler

    def unregister_byte_stream_handler(self, topic):
        self._byte_stream_handlers.pop(topic, None)

    # ─── Session lifecycle ───────────────────────────────────────────────

    def _on_session_ended(self):
        """Called when the call/stream ends — stop recording, emit disconnected.

        participant_disconnected is emitted separately by the server event loop
        when call_terminated arrives (matching LiveKit's RoomIO pattern where
        the room fires participant_disconnected and RoomIO handles it).
        """
        self._connected = False
        # Stop Rust recording if active
        ep = self._ep
        session_id = self._sid
        if ep and session_id:
            try:
                ep.stop_recording(session_id)
            except Exception:
                pass
        self.emit("disconnected")


# ─── Stub Job Context ────────────────────────────────────────────────────────

@dataclass
class _StubJob:
    """Minimal stub for agent.Job protobuf — provides fields AgentSession reads."""
    id: str
    agent_name: str
    enable_recording: bool = False


class _StubJobContext:
    """Minimal stub for JobContext — provides .room, .job, and other fields
    that AgentSession.start() accesses via get_job_context().

    Not a full JobContext — just enough to avoid RuntimeError and AttributeError.
    """

    def __init__(self, room: TransportRoom, agent_name: str = "agent"):
        self._room = room
        self._job = _StubJob(id=f"job-{room._sid}", agent_name=agent_name)
        self._primary_agent_session = None
        self._shutdown_callbacks: list = []
        self._tempdir = tempfile.TemporaryDirectory()
        self.session_directory = Path(self._tempdir.name)
        self.worker_id = "local"

    @property
    def room(self):
        return self._room

    @property
    def job(self):
        return self._job

    @property
    def proc(self):
        return self  # Self-stub for proc.executor_type check

    @property
    def executor_type(self):
        return None  # Avoids _ContextLogFieldsFilter match

    def is_fake_job(self) -> bool:
        return False

    @property
    def inference_executor(self):
        """Return the inference executor if one was set up."""
        return getattr(self, '_inf_executor', None)

    def init_recording(self, options):
        """Called by AgentSession when record=True is passed to session.start().

        Starts Rust-level recording (stereo WAV) directly from the transport
        send/recv loops — zero Python overhead, no per-frame copying.

        Also disables RecorderIO's Python-level recording to avoid double
        recording. Rust recording is more efficient for production.
        """
        if not options.get("audio", False):
            return

        ep = self._room._ep if self._room else None
        session_id = self._room._sid if self._room else None
        if ep is None or session_id is None:
            return

        try:
            import os
            rec_dir = str(self.session_directory)
            os.makedirs(rec_dir, exist_ok=True)
            rec_path = os.path.join(rec_dir, "audio.ogg")
            ep.start_recording(session_id, rec_path, True)  # stereo OGG/Opus
            logger.debug("Recording started (Rust OGG/Opus): %s", rec_path)

            # Disable RecorderIO's Python-level recording — Rust handles it
            options["audio"] = False
        except Exception:
            logger.warning("Rust recording failed, falling back to RecorderIO", exc_info=True)
            # Don't disable RecorderIO — let it handle recording as fallback

    async def connect(self):
        pass

    async def _on_session_end(self):
        """Called when session ends — stop Rust recording if active."""
        ep = self._room._ep if self._room else None
        session_id = self._room._sid if self._room else None
        if ep and session_id:
            try:
                ep.stop_recording(session_id)
            except Exception:
                pass

    def add_shutdown_callback(self, callback):
        self._shutdown_callbacks.append(callback)

    def shutdown(self, reason: str = ""):
        pass

    async def delete_room(self, room_name=""):
        pass

    @property
    def log_context_fields(self):
        return {}

    @property
    def primary_session(self):
        return self._primary_agent_session

    @property
    def local_participant_identity(self):
        return self._room._local_participant.identity if self._room else ""

    def make_session_report(self, *args, **kwargs):
        pass

    @property
    def tagger(self):
        return None

    def token_claims(self):
        return {}

    @property
    def api(self):
        return None

    @property
    def agent(self):
        return None


def create_transport_context(room: TransportRoom, agent_name: str = "agent",
                             inference_executor=None) -> tuple:
    """Create a stub JobContext and set it on _JobContextVar.

    Returns (stub_context, context_token) — caller must reset token on cleanup.

    Usage:
        ctx, token = create_transport_context(room, agent_name)
        try:
            await session.start(agent=agent)
        finally:
            _JobContextVar.reset(token)
    """
    from livekit.agents.job import _JobContextVar

    stub = _StubJobContext(room=room, agent_name=agent_name)
    if inference_executor:
        stub._inf_executor = inference_executor
    token = _JobContextVar.set(stub)
    return stub, token
