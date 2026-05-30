"""TransportRoom — facade implementing rtc.Room interface for SIP/AudioStream transport.

Makes get_job_context().room work so existing LiveKit agent code (GetDtmfTask,
SendDtmfTool, background audio, transcription, warm transfer) runs unchanged.

Architecture:
- TransportRoom extends rtc.EventEmitter (same base as rtc.Room)
- _TransportLocalParticipant maps publish_dtmf → ep.send_dtmf, etc.
- TransportJobContextMixin provides .room, .job and LiveKit-compatible
  JobContext helpers for AgentSession's get_job_context() calls
- Server event loop routes DTMF events → room.emit("sip_dtmf_received", SipDTMF(...))
"""

import asyncio
import datetime
import inspect
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from livekit import rtc
from livekit.rtc.event_emitter import EventEmitter

from ._aio_utils import control_executor as _control_executor
from ._aio_utils import schedule_hangup

logger = logging.getLogger(__name__)


# ─── Stub track publication (returned by publish_track) ──────────────────────

class _StubTrackPublication:
    """Stub for rtc.LocalTrackPublication.

    Default kind/source are AUDIO/MICROPHONE because this transport only
    publishes the agent's synthesized voice. Callers that publish a
    different kind (e.g., background audio from a LocalAudioTrack) can
    still distinguish them via the `track` reference; `kind` and `source`
    are parameterised so non-default sources (e.g. SCREEN_SHARE_AUDIO)
    round-trip correctly even though we don't physically emit them.
    """

    def __init__(self, track=None, sid=None, *, kind: int = 0, source: int = 1,
                 mime_type: str = "audio/opus"):
        self.track = track
        self.sid = sid or f"TR_{uuid.uuid4().hex[:8]}"
        self.name = ""
        self.kind = kind          # 0 = AUDIO (default; we only handle audio)
        self.source = source      # 1 = MICROPHONE
        self.muted = False
        self.simulcasted = False
        self.width = 0
        self.height = 0
        self.mime_type = mime_type
        self.encryption_type = 0
        self.audio_features = []

    async def wait_for_subscription(self):
        """No-op. Our transport has no remote WebRTC subscriber to wait for;
        the SIP/audio-stream peer is implicitly the only receiver and is
        already active by the time this publication exists.
        """
        pass


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
        """Send DTMF — maps to ep.send_dtmf().

        Polymorphic across transports:

        - **SIP (RTP)**: the Rust FFI is a `runtime.block_on` wrapper
          around `RtpTransport.send_dtmf_event`, which paces RFC 4733
          events over ~280 ms per digit (1 start + 10 continue + 3
          end-of-event packets × 20 ms ptime). Running this
          synchronously from an async context would block the asyncio
          loop for that entire window (and ~1 s on a 4-digit PIN).

        - **audio_stream (Plivo WS)**: `send_dtmf` is a non-blocking
          WebSocket send that emits `{"event": "sendDTMF", "dtmf": X}`
          to Plivo. It takes microseconds, not hundreds of ms.

        We `run_in_executor` unconditionally — harmless for the fast
        audio_stream path (one thread hop), essential for the slow
        SIP path so the loop can keep processing audio/STT frames
        while RTP DTMF events are paced on the Rust side.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._ep.send_dtmf, self._sid, digit)

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
        sr = self._ep.input_sample_rate if self._ep is not None else 8000
        try:
            stream = rtc.AudioStream.from_track(
                track=track, sample_rate=sr, num_channels=1)
        except Exception:
            logger.warning("Track forwarding: failed to create AudioStream for %s", pub_sid, exc_info=True)
            return

        logger.debug("Forwarding published track %s to background mixer", pub_sid)
        frame_count = 0
        try:
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
                    except Exception:
                        break  # Session gone — forwarding task will be cancelled by _on_session_ended
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Track forwarding loop ended for %s", pub_sid, exc_info=True)
        finally:
            # Always close the native AudioStream on exit so the livekit-ffi
            # reader task is released. Without this, an exception inside the
            # send_background_audio path leaks a subscribed native reader that
            # keeps accumulating frames until the process exits.
            try:
                await stream.aclose()
            except Exception:
                pass

    async def publish_transcription(self, transcription) -> None:
        """No-op on SIP/audio_stream transport.

        In LiveKit WebRTC, `publish_transcription` sends per-word transcript
        updates to every participant subscribed to this track via the signal
        channel. Neither SIP nor Plivo audio_stream has a side channel to
        deliver text to the remote peer — the agent's spoken words ARE the
        transcript, and they're already on the wire as audio.

        Agents that want their own transcripts (for logging, state tracking,
        handoffs) should read `AgentSession.on("transcription_update")` or
        similar, which fires regardless of whether the transcription is
        actually broadcast.
        """
        pass

    async def stream_text(self, *, destination_identities=None, topic="",
                          attributes=None, stream_id=None, reply_to_id=None,
                          total_size=None, sender_identity=None, **kw):
        """No-op text stream writer.

        LiveKit WebRTC exposes `stream_text` for streaming arbitrary text
        payloads to other participants via the data channel. SIP has no
        data channel; Plivo audio_stream has a JSON control channel but no
        chunked-write semantics. Returns a stub writer that accepts writes
        and drops them on the floor.
        """
        return _StubTextStreamWriter()

    async def send_text(self, text, *, destination_identities=None, topic="",
                        attributes=None, reply_to_id=None):
        """No-op — see `stream_text` for why this has no wire implementation.

        A future enhancement could route `send_text` through SIP MESSAGE
        (RFC 3428) for outbound text-to-pstn, but no current consumer
        depends on that.
        """
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

    async def perform_rpc(self, *, destination_identity, method, payload,
                          response_timeout=None):
        """RPC over data channels — not supported on SIP transport.

        LiveKit's `LocalParticipant.perform_rpc` sends a request over the
        WebRTC data channel and awaits a typed response. SIP has no data
        channel; the closest analogs are SIP INFO (for control messages,
        already exposed via `send_info`) and HTTP (use the embedded HTTP
        server in `AgentServer` instead).

        Raising `NotImplementedError` here surfaces the limitation early
        instead of silently returning an empty string and letting the
        caller treat that as a successful (empty) response.
        """
        raise NotImplementedError(
            "perform_rpc is not supported on SIP transport — use SIP INFO "
            "(send_info) or HTTP for control messages instead."
        )

    async def stream_bytes(self, name, **kw):
        """No-op byte stream writer — see `stream_text` for rationale."""
        return _StubTextStreamWriter()  # Close enough interface


# ─── Transport Remote Participant ────────────────────────────────────────────

class _TransportRemoteParticipant:
    """Stub for the remote caller.

    `kind` defaults to 3 (PARTICIPANT_KIND_SIP) because the original
    TransportRoom was SIP-only, but is configurable so the audio_stream
    adapter can report 0 (STANDARD) when connecting a Plivo WS peer —
    Plivo audio_stream sessions are not technically SIP.
    """

    def __init__(self, identity, session_id, *, kind: int = 3):
        self.sid = f"PR_{session_id}"
        self.identity = identity
        self.name = identity
        self.metadata = ""
        self.attributes: dict[str, str] = {}
        self.kind = kind  # 3 = PARTICIPANT_KIND_SIP, 0 = PARTICIPANT_KIND_STANDARD
        self.permissions = None
        self.disconnect_reason = None
        self.track_publications: dict = {}


# ─── Transport Room ──────────────────────────────────────────────────────────

class TransportRoom(EventEmitter):
    """Facade Room wrapping SipEndpoint/AudioStreamEndpoint.

    Extends rtc.EventEmitter so on()/off()/emit() work exactly like rtc.Room.
    LiveKit agents code that does room.on("sip_dtmf_received", handler) works unchanged.
    """

    def __init__(self, endpoint, session_id, *, agent_name, caller_identity,
                 remote_kind: int = 3):
        super().__init__()
        self._ep = endpoint
        self._sid = session_id
        self._connected = True

        self._local_participant = _TransportLocalParticipant(
            endpoint, session_id, agent_name)
        # remote_kind: 3 = SIP (default), 0 = STANDARD (audio_stream/Plivo).
        self._remote = _TransportRemoteParticipant(
            caller_identity, str(session_id), kind=remote_kind)
        self._remote_participants = {caller_identity: self._remote}
        self._name = f"transport-{session_id}"
        self._creation_time = datetime.datetime.now(datetime.timezone.utc)
        self._text_stream_handlers: dict[str, Any] = {}
        self._byte_stream_handlers: dict[str, Any] = {}
        # Guards against emitting the rtc.Room "disconnected" event more than
        # once. Both disconnect() (agent-initiated) and _on_session_ended()
        # (server-initiated teardown) can run for the same call; the rtc.Room
        # contract is that "disconnected" fires exactly once.
        self._disconnected_emitted = False

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

    def _emit_disconnected_once(self):
        """Emit the rtc.Room "disconnected" event at most once.

        disconnect() (agent code calling room.disconnect()) and
        _on_session_ended() (server teardown when the call ends) can both run
        for a single call. LiveKit's rtc.Room fires "disconnected" exactly
        once; mirror that so listeners (e.g. RoomIO) aren't double-notified.
        """
        if self._disconnected_emitted:
            return
        self._disconnected_emitted = True
        self.emit("disconnected")

    async def disconnect(self):
        self._connected = False
        self._emit_disconnected_once()

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
        """Called when the call/stream ends — stop recording, cancel forwarding tasks, emit disconnected.

        participant_disconnected is emitted separately by the server event loop
        when call_terminated arrives (matching LiveKit's RoomIO pattern where
        the room fires participant_disconnected and RoomIO handles it).
        """
        self._connected = False
        # Cancel background audio forwarding tasks owned by the local participant
        # (matches LiveKit's track unpublish on disconnect). The tasks dict lives
        # on the participant, not the room.
        lp = self._local_participant
        tasks = getattr(lp, "_track_forward_tasks", None)
        if tasks:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            tasks.clear()
        # Stop Rust recording if active
        ep = self._ep
        session_id = self._sid
        if ep and session_id:
            try:
                ep.stop_recording(session_id)
            except Exception:
                pass
        self._emit_disconnected_once()


# ─── Stub Job Context ────────────────────────────────────────────────────────

@dataclass
class _StubJobRoom:
    """Minimal stub for job.room — provides .sid for inference headers."""
    sid: str = ""
    name: str = ""


@dataclass
class _StubJob:
    """Minimal stub for agent.Job protobuf — provides fields AgentSession reads."""
    id: str
    agent_name: str
    room: _StubJobRoom | None = None
    enable_recording: bool = True


class _NoopTagger:
    """No-op shim for livekit.agents Tagger.

    LiveKit's `JobContext.tagger` returns a real Tagger instance for cloud
    eval/analytics. We don't have cloud connectivity, so user code that
    calls ctx.tagger.success() / .fail() / .add() / ._evaluation() should
    not crash. This shim accepts any method call and silently no-ops.
    """

    def __getattr__(self, name):
        # Any method call returns a no-op callable that accepts anything.
        def _noop(*args, **kwargs):
            return None
        return _noop


class TransportJobContextMixin:
    """LiveKit-compatible JobContext surface for transport-backed contexts.

    Provides .room, .job, and the other fields/helpers that
    AgentSession.start() accesses via get_job_context(). Mixed into the
    public dataclass JobContexts (SIP / audio_stream) so the entrypoint's
    context IS the canonical job context, and kept as the base of
    _StubJobContext for focused tests and setup shims.
    """

    def _init_transport_job_context(
        self,
        room: TransportRoom,
        agent_name: str = "agent",
        *,
        enable_recording: bool = True,
        inference_executor=None,
    ) -> None:
        self._room = room
        self._job = _StubJob(
            id=f"job-{room._sid}",
            agent_name=agent_name,
            room=_StubJobRoom(sid=room.sid, name=room.name),
            enable_recording=enable_recording,
        )
        # Override _job.room with the real TransportRoom (mirrors LiveKit
        # JobContext where ctx.job.room IS the live rtc.Room). Code that
        # navigates `ctx.job.room.local_participant.publish_track(...)`
        # works the same as `ctx.room.local_participant.publish_track(...)`.
        self._job.room = self._room
        self._primary_agent_session = None
        self._shutdown_callbacks: list = []
        self._shutdown_callbacks_fired = False
        # Strong refs to fire-and-forget async shutdown-callback tasks so the
        # event loop doesn't GC them mid-run (asyncio holds only weak refs).
        self._shutdown_tasks: set = set()
        self.session_directory = Path("/tmp/agent-sessions")
        self.session_directory.mkdir(parents=True, exist_ok=True)
        self.worker_id = "local"
        # When mixed into a dataclass JobContext that still carries a
        # legacy `_job_stub` field, point it at self so the context is its
        # own job stub (single identity).
        if hasattr(self, "_job_stub"):
            self._job_stub = self
        if inference_executor:
            self._inf_executor = inference_executor
        # Storage for ctx.log_context_fields (also accessed as
        # ctx._log_fields by LiveKit's internal _ContextLogFieldsFilter,
        # though we don't install that filter ourselves).
        self._log_fields: dict = {}
        self._tagger = _NoopTagger()

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

        Starts Rust-level recording (stereo OGG/Opus) directly from the
        transport send/recv loops — zero Python overhead, zero per-frame
        copying, zero GIL hold per frame (the encoder runs on a dedicated
        OS thread inside Rust).

        Also disables RecorderIO's Python-level recording to avoid double
        recording. Rust recording is more efficient for production.

        IMPORTANT — side effect on `options`: we set ``options["audio"] =
        False`` on the *live* dict we're handed. This is load-bearing, not a
        bug: LiveKit's AgentSession reads ``self._recording_options["audio"]``
        from this exact dict (voice/agent_session.py) to decide whether to
        wire up RecorderIO. Copying the dict and mutating the copy would NOT
        suppress RecorderIO, so the agent would record twice (once in Rust,
        once in Python). The only way to disable RecorderIO is to clear the
        flag on the dict LiveKit actually inspects. We capture the original
        value and restore it in ``_on_session_end`` so the dict round-trips
        across the session lifecycle and nothing downstream sees a leaked
        ``audio: False``.
        """
        if not options.get("audio", False):
            return

        ep = self._room._ep if self._room else None
        session_id = self._room._sid if self._room else None
        if ep is None or session_id is None:
            return

        # Rust recording: stereo OGG/Opus at the transport layer.
        # Captures agent voice + background audio + user audio (all mixed).
        # The Rust call is fast (just creates the encoder state); the actual
        # encoding runs on a dedicated OS thread inside Rust without GIL hold.
        try:
            import os
            rec_dir = str(self.session_directory)
            os.makedirs(rec_dir, exist_ok=True)
            rec_path = os.path.join(rec_dir, f"recording_{session_id}.ogg")
            ep.start_recording(session_id, rec_path, True)
            logger.debug("Recording started (Rust OGG/Opus): %s", rec_path)
            # Disable RecorderIO — Rust handles recording with full audio mix.
            # Save the original so we can restore on session end (see
            # _on_session_end below).
            self._original_audio_recording_flag = options.get("audio")
            self._recording_options_ref = options
            options["audio"] = False
        except Exception:
            logger.warning("Rust recording failed, falling back to RecorderIO", exc_info=True)

    async def connect(self):
        """No-op — no real room to connect to.

        Mirrors LiveKit's `JobContext.connect()` interface, but our
        SIP/audio_stream call is already up by the time the JobContext
        is created (it was created from the inbound INVITE / outbound
        ep.call() that already completed). Returns immediately.
        """
        return None

    async def _on_session_end(self):
        """Called when session ends — stop Rust recording and restore options."""
        ep = self._room._ep if self._room else None
        session_id = self._room._sid if self._room else None
        if ep and session_id:
            try:
                # stop_recording is fast (just signals the encoder thread).
                ep.stop_recording(session_id)
            except Exception:
                pass
        # Restore the user's `options["audio"]` flag if init_recording
        # mutated it. The caller's dict round-trips cleanly across the
        # session lifecycle.
        opts_ref = getattr(self, "_recording_options_ref", None)
        if opts_ref is not None and hasattr(self, "_original_audio_recording_flag"):
            try:
                if self._original_audio_recording_flag is None:
                    opts_ref.pop("audio", None)
                else:
                    opts_ref["audio"] = self._original_audio_recording_flag
            except Exception:
                pass

    def add_shutdown_callback(self, callback):
        """Register a callback to fire on job shutdown.

        Mirrors LiveKit's JobContext.add_shutdown_callback signature
        normalization: callbacks may be sync or async and may accept either
        zero arguments or the shutdown reason. The stored list always
        contains `def(reason: str)` wrappers so they can be called
        uniformly from shutdown(reason) / _run_shutdown_callbacks(reason).
        """
        import inspect
        min_args_num = 2 if inspect.ismethod(callback) else 1
        takes_reason = (
            hasattr(callback, "__code__")
            and callback.__code__.co_argcount >= min_args_num
        )

        def _wrapper(reason: str):
            return callback(reason) if takes_reason else callback()

        self._shutdown_callbacks.append(_wrapper)

    def _take_shutdown_callbacks(self):
        """Return the registered callbacks once, then mark as fired.

        Guarantees once-only dispatch: shutdown() and the server's final
        cleanup both call this, but only the first wins.
        """
        if getattr(self, "_shutdown_callbacks_fired", False):
            return []
        self._shutdown_callbacks_fired = True
        return list(self._shutdown_callbacks)

    @staticmethod
    def _invoke_shutdown_callback(cb, reason: str):
        """Invoke one shutdown callback and return its pending awaitable, or
        ``None`` if it was a plain sync callback (or raised). Per-callback errors
        are logged and swallowed so one bad callback can't block the rest.

        Shared by the sync ``shutdown()`` and async ``_run_shutdown_callbacks()``
        paths so the invoke / await-detection logic can't drift between them —
        each path only differs in how it disposes of the returned awaitable
        (await it vs. fire-and-forget on the loop).
        """
        try:
            result = cb(reason)
        except Exception:
            logger.debug("shutdown callback failed", exc_info=True)
            return None
        return result if inspect.isawaitable(result) else None

    async def _run_shutdown_callbacks(self, reason: str = "") -> None:
        """Run shutdown callbacks once, awaiting any async ones."""
        for cb in self._take_shutdown_callbacks():
            coro = self._invoke_shutdown_callback(cb, reason)
            if coro is not None:
                try:
                    await coro
                except Exception:
                    logger.debug("async shutdown callback failed", exc_info=True)

    def shutdown(self, reason: str = ""):
        """Terminate the agent job and drop the underlying SIP/audio_stream call.

        Called by LiveKit's EndCallTool (via job_ctx.shutdown(reason=...)) and
        by user code that wants to abort the agent session. In a real LiveKit
        deployment this terminates the job process and indirectly drops the
        SIP caller via room teardown — we don't have rooms, so we explicitly
        hangup the call here. Idempotent: hangup on an already-terminated
        call is a no-op in the Rust core.
        """
        logger.info("JobContext.shutdown(reason=%r) — dropping call %s", reason, self._room._sid if self._room else "?")
        ep = self._room._ep if self._room else None
        session_id = self._room._sid if self._room else None
        if ep and session_id:
            # hangup() is a Rust block_on (~50-200ms talking to Plivo/SIP).
            # shutdown() is synchronous per LiveKit's EndCallTool contract and
            # runs on the asyncio loop thread, so schedule the hangup off-loop
            # (dedicated control executor) to avoid stalling other sessions for
            # the network round-trip. Fire-and-forget; shutdown() must return
            # synchronously. The async sibling delete_room() does the same.
            schedule_hangup(ep.hangup, session_id)
        # Fire user-registered shutdown callbacks once. LiveKit calls them
        # with the reason string; add_shutdown_callback normalized the shape.
        # _take_shutdown_callbacks() guarantees once-only dispatch across
        # both shutdown() and the server's final cleanup.
        for cb in self._take_shutdown_callbacks():
            coro = self._invoke_shutdown_callback(cb, reason)
            if coro is not None:
                # shutdown() is synchronous per LiveKit's contract, so async
                # user cleanup runs fire-and-forget on the running loop.
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    self._spawn_retained(loop, coro)
                else:
                    coro.close()  # no loop to run it on — don't leak the coroutine

    def _spawn_retained(self, loop: "asyncio.AbstractEventLoop", coro) -> None:
        """Schedule ``coro`` fire-and-forget while holding a strong reference to
        the task, so the event loop can't GC it before it finishes (asyncio keeps
        only a weak ref to tasks). The task removes itself on completion and its
        exception is retrieved so it's never flagged as unretrieved."""
        if not hasattr(self, "_shutdown_tasks"):
            self._shutdown_tasks = set()
        task = loop.create_task(coro)
        self._shutdown_tasks.add(task)

        def _done(t: "asyncio.Task") -> None:
            self._shutdown_tasks.discard(t)
            if not t.cancelled():
                t.exception()  # retrieve so it isn't reported as unretrieved

        task.add_done_callback(_done)

    async def delete_room(self, room_name=""):
        """Drop the underlying SIP/audio_stream call.

        LiveKit's EndCallTool calls this (via add_shutdown_callback ->
        job_ctx.delete_room()) to disconnect remote SIP callers by deleting
        the WebRTC room. We don't have rooms, so this maps directly to
        ep.hangup(). Idempotent — safe to call multiple times.

        Note: LiveKit's real signature returns asyncio.Future, not async def,
        but `await job_ctx.delete_room()` works correctly with both forms
        (await on a coroutine and await on a future are interchangeable
        from the caller's perspective).
        """
        logger.info("JobContext.delete_room — dropping call %s", self._room._sid if self._room else "?")
        ep = self._room._ep if self._room else None
        session_id = self._room._sid if self._room else None
        if ep and session_id:
            try:
                # ep.hangup is a Rust block_on call (~50-200ms talking to
                # the SIP proxy). Run on the dedicated call-control executor so
                # the asyncio loop isn't blocked while Rust talks to the network
                # and the audio forwarders' default pool isn't contended.
                import asyncio as _asyncio
                loop = _asyncio.get_running_loop()
                await loop.run_in_executor(_control_executor(), ep.hangup, session_id)
            except Exception:
                logger.debug("hangup during JobContext.delete_room failed", exc_info=True)

    async def wait_for_participant(self, *, identity=None, kind=None):
        """Returns the (single) remote participant for this SIP/audio_stream call.

        Mirrors LiveKit's `JobContext.wait_for_participant` (job.py:467-479).
        In WebRTC this awaits the next participant join; for our facade we
        always have exactly one remote (the SIP caller / Plivo stream peer)
        which is created during TransportRoom init, so we return immediately.

        If `identity` is given and doesn't match our remote's identity, this
        still returns the remote (matching upstream's "first matching" semantics
        is overkill for single-participant transport — log a warning instead).
        """
        if self._room is None:
            raise RuntimeError("JobContext has no room")
        remote = self._room._remote
        if identity is not None and remote.identity != identity:
            logger.warning(
                "wait_for_participant(identity=%r) doesn't match remote %r — "
                "single-participant SIP transport returns the only remote anyway",
                identity, remote.identity,
            )
        return remote

    def add_sip_participant(
        self,
        *,
        call_to: str,
        trunk_id=None,
        participant_identity: str,
        participant_name=None,
        **kwargs,
    ):
        """Place an outbound SIP call.

        Mirrors LiveKit's `JobContext.add_sip_participant` (job.py:539-580).
        In real LiveKit this dials via the SIP service trunk; here we
        forward directly to `ep.call(call_to, ...)`. The `trunk_id` arg
        is ignored (we have no LiveKit trunks).

        Returns an asyncio.Future that resolves to a `_FakeSipParticipantInfo`
        once the call is connected. Matches the upstream `Future[SIPParticipantInfo]`
        return type — both are awaitable.
        """
        import asyncio as _asyncio
        from collections import namedtuple
        _FakeSipParticipantInfo = namedtuple(
            "FakeSipParticipantInfo",
            ["participant_identity", "participant_name", "sip_call_id", "room_name"],
        )

        async def _dial():
            if self._room is None or self._room._ep is None:
                raise RuntimeError("JobContext has no endpoint to dial from")
            loop = _asyncio.get_running_loop()
            # ep.call is a Rust block_on (INVITE → ringing → 200 OK), can take
            # several seconds — must run in executor so the asyncio loop isn't
            # blocked. The actual SIP signaling happens in Rust.
            try:
                session_id = await loop.run_in_executor(
                    None, self._room._ep.call, call_to, None, None
                )
                logger.info(
                    "JobContext.add_sip_participant: outbound call %s -> %s connected",
                    session_id, call_to,
                )
            except Exception as e:
                logger.warning("JobContext.add_sip_participant failed: %s", e)
                raise
            return _FakeSipParticipantInfo(
                participant_identity=participant_identity,
                participant_name=participant_name or participant_identity,
                sip_call_id=session_id,
                room_name=self._room.name if self._room else "",
            )

        return _asyncio.ensure_future(_dial())

    def transfer_sip_participant(
        self,
        participant,
        transfer_to: str,
        *,
        play_dialtone: bool = False,
        **kwargs,
    ):
        """Transfer the current SIP call to a different destination via REFER.

        Mirrors LiveKit's `JobContext.transfer_sip_participant` (job.py:582-632).
        Forwards to `ep.transfer(call_id, transfer_to)` which sends a SIP REFER.
        The `participant` arg is accepted for API parity but ignored — there's
        only one remote in our single-participant facade.

        Returns an asyncio.Future per upstream contract.
        """
        import asyncio as _asyncio

        async def _transfer():
            if self._room is None or self._room._ep is None:
                raise RuntimeError("JobContext has no endpoint to transfer from")
            loop = _asyncio.get_running_loop()
            # ep.transfer is a Rust block_on that sends SIP REFER and
            # waits for the response. Run in executor.
            try:
                await loop.run_in_executor(
                    None, self._room._ep.transfer, self._room._sid, transfer_to
                )
                logger.info(
                    "JobContext.transfer_sip_participant: %s -> %s",
                    self._room._sid, transfer_to,
                )
            except Exception as e:
                logger.warning("JobContext.transfer_sip_participant failed: %s", e)
                raise

        return _asyncio.ensure_future(_transfer())

    def add_participant_entrypoint(self, entrypoint_fnc, *_args, **_kwargs):
        """Register a per-participant entrypoint (LiveKit JobContext API).

        In WebRTC this fires once per participant joining a room. SIP /
        audio_stream is single-participant by definition (one caller per
        call), so we either fire it immediately for the existing remote
        or store it as a no-op. We choose to fire it immediately so agent
        code that uses this pattern still works.

        The fn is called with (ctx, participant). We don't await its
        result — fire-and-forget like LiveKit does internally.
        """
        import asyncio as _asyncio
        if self._room is None:
            logger.warning("add_participant_entrypoint called without a room")
            return
        remote = self._room._remote
        try:
            result = entrypoint_fnc(self, remote)
            if result is not None and hasattr(result, "__await__"):
                # Async entrypoint — schedule on loop, store strong ref to avoid GC
                if not hasattr(self, "_participant_entrypoint_tasks"):
                    self._participant_entrypoint_tasks = set()
                try:
                    loop = _asyncio.get_event_loop()
                    if loop.is_running():
                        t = loop.create_task(result)
                        self._participant_entrypoint_tasks.add(t)
                        t.add_done_callback(self._participant_entrypoint_tasks.discard)
                except Exception:
                    logger.debug("add_participant_entrypoint: failed to schedule", exc_info=True)
        except Exception:
            logger.exception("add_participant_entrypoint: entrypoint raised")

    @property
    def log_context_fields(self) -> dict:
        """Returns the dict of structured log fields. Mutable.

        Mirrors LiveKit's contract: callers can either edit the returned
        dict in place or replace it via `ctx.log_context_fields = {...}`.
        We don't actually install LiveKit's _ContextLogFieldsFilter (it
        requires a real worker setup), so the fields aren't injected
        into log records — but the getter/setter still need to exist
        so user code that does `ctx.log_context_fields = {...}` doesn't
        crash with AttributeError.
        """
        return self._log_fields

    @log_context_fields.setter
    def log_context_fields(self, fields: dict) -> None:
        self._log_fields = fields

    @property
    def primary_session(self):
        return self._primary_agent_session

    @property
    def local_participant_identity(self):
        return self._room._local_participant.identity if self._room else ""

    def make_session_report(self, *args, **kwargs):
        # LiveKit uses this for cloud session telemetry. We don't ship to
        # LiveKit Cloud, so this is a no-op. Tools that read the return
        # value should not depend on it being non-None.
        return None

    @property
    def tagger(self):
        """Returns a no-op Tagger shim.

        LiveKit's Tagger sends success/fail/eval signals to LiveKit Cloud.
        We don't have cloud connectivity, so any user code that calls
        `ctx.tagger.success()` / `.fail()` / `.add()` / `._evaluation()`
        gets a silent no-op via `_NoopTagger.__getattr__`. Returning None
        would crash on attribute access; the shim is required for parity.
        """
        return self._tagger

    def token_claims(self):
        return {}

    @property
    def api(self):
        # LiveKit's `JobContext.api` returns a LiveKitAPI client for room
        # control / SIP outbound / data publishing. We don't have one,
        # so callers that touch ctx.api will get None and need to guard.
        # Code in EndCallTool / RoomIO doesn't use ctx.api directly.
        return None

    @property
    def agent(self):
        """Returns the local participant — shorthand for ctx.room.local_participant.

        Mirrors LiveKit JobContext.agent at job.py:406. Some agent code uses
        `ctx.agent.publish_track(...)` as shorthand. Returning None here
        would crash; we forward to the local participant.
        """
        return self._room.local_participant if self._room else None


class _StubJobContext(TransportJobContextMixin):
    """Standalone JobContext facade kept for focused tests and setup shims.

    The public dataclass JobContexts (SIP / audio_stream) now mix in
    TransportJobContextMixin directly so the entrypoint context IS the
    canonical job context; this standalone wrapper is the fallback used
    when no context is supplied to create_transport_context().
    """

    def __init__(
        self,
        room: TransportRoom,
        agent_name: str = "agent",
        *,
        enable_recording: bool = True,
    ):
        self._init_transport_job_context(
            room,
            agent_name,
            enable_recording=enable_recording,
        )


def create_transport_context(room: TransportRoom, agent_name: str = "agent",
                             inference_executor=None, *,
                             enable_recording: bool = True,
                             context: "TransportJobContextMixin | None" = None) -> tuple:
    """Set a transport-compatible JobContext on _JobContextVar.

    Returns (job_context, context_token) — caller must reset token on cleanup.

    When ``context`` is supplied (the public dataclass JobContext), its
    LiveKit-compatible surface is initialized in place and it becomes the
    canonical context — so the entrypoint's ctx IS get_job_context(). When
    ``context`` is None, a standalone _StubJobContext is created (used by
    focused tests / setup shims).

    Usage:
        ctx, token = create_transport_context(room, agent_name)
        try:
            await session.start(agent=agent)
        finally:
            _JobContextVar.reset(token)
    """
    from livekit.agents.job import _JobContextVar

    if context is None:
        context = _StubJobContext(
            room=room,
            agent_name=agent_name,
            enable_recording=enable_recording,
        )
        if inference_executor:
            context._inf_executor = inference_executor
    else:
        context._init_transport_job_context(
            room,
            agent_name,
            enable_recording=enable_recording,
            inference_executor=inference_executor,
        )
    token = _JobContextVar.set(context)
    return context, token
