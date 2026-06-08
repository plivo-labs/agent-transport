"""AudioStreamServer — drop-in equivalent of AgentServer for Plivo audio streaming.

Same pattern as AgentServer but over WebSocket instead of SIP:
    server = AudioStreamServer(listen_addr="0.0.0.0:8765")

    @server.audio_stream_session()
    async def entrypoint(ctx: JobContext):
        session = AgentSession(vad=..., stt=..., llm=..., tts=...)
        await ctx.start(session, agent=Assistant())

    if __name__ == "__main__":
        server.run()

No SIP credentials needed — Plivo connects to your WebSocket server.
Configure Plivo XML to return:
    <Response>
        <Stream bidirectional="true" keepCallAlive="true"
            contentType="audio/x-mulaw;rate=8000">
            wss://your-server:8765
        </Stream>
    </Response>

The shared server machinery (inference bootstrap, load monitor, HTTP
surface, FfiQueue lifecycle loop, force-exit cleanup) lives in
``_server_base.AgentServerBase``. This file carries only the
audio-stream specifics: WebSocket endpoint creation and the
fire-and-forget Plivo hangup. Shutdown model: see ``server.py``.
"""

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

import prometheus_client

from agent_transport import AudioStreamEndpoint
from agent_transport._event import FfiEvent
from agent_transport._event_sink import _on_event_from_rust
from agent_transport._ffi_queue import GLOBAL
from ._room_facade import TransportJobContextMixin, TransportRoom, create_transport_context
from ._session_finalize import finalize_session
from ._server_base import AgentServerBase, JobContextBase, _nodename
from .judging import EvaluationConfig

logger = logging.getLogger("agent_transport.audio_stream_server")

# ─── Audio-stream-specific Prometheus metrics ─────────────────────────────────

STREAM_SESSIONS_TOTAL = prometheus_client.Counter(
    "lk_agents_audio_stream_sessions_total",
    "Total audio stream sessions handled",
    ["nodename"],
)

STREAM_SESSION_DURATION = prometheus_client.Histogram(
    "lk_agents_audio_stream_session_duration_seconds",
    "Audio stream session duration in seconds",
    ["nodename"],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600],
)


@dataclass
class JobContext(JobContextBase, TransportJobContextMixin):
    """Context passed to the @audio_stream_session handler.

    Setting ctx.session automatically wires audio stream I/O and registers the
    close handler; then ``session.start(room=ctx.room)`` works exactly like
    LiveKit WebRTC. The shared method surface (session wiring, observability
    tagging, listener registry) lives in :class:`JobContextBase`.

    DTMF events (equivalent of room.on("sip_dtmf_received") in WebRTC):
        job_ctx = get_job_context()
        job_ctx.room.on("sip_dtmf_received", handler)
    """

    # JobContextBase hooks (plain class attrs — not dataclass fields).
    _transport_tag = "audio_stream"
    _unit_label = "Session"
    _ctx_logger = logger
    _debug_logger_name = "agent_transport.audio_stream"

    session_id: str
    plivo_call_uuid: str      # Plivo Call UUID
    stream_id: str            # Plivo Stream UUID
    direction: str            # Always "inbound" for audio streams
    extra_headers: dict[str, str]
    endpoint: AudioStreamEndpoint
    userdata: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    """Session metadata to attach to native LiveKit observability tags."""
    account_id: str | None = None
    """Account ID for multi-tenancy — set by the consumer per session."""
    evaluation: EvaluationConfig | None = None
    """Post-conversation evaluation config for this session."""

    _agent_name: str = field(default="agent", repr=False)
    # Stable developer-supplied id (typically UUID4). Set by the
    # AudioStreamServer when creating the JobContext per session and threaded
    # through to the observability emitter.
    _agent_id: str = field(default="", repr=False)
    _session: Any = field(default=None, repr=False)
    _call_ended: asyncio.Event | None = field(default=None, repr=False)
    _room: Any = field(default=None, repr=False)
    _job_ctx_token: Any = field(default=None, repr=False)
    _event_listeners: dict = field(default_factory=dict, repr=False)
    # 0.2.x post-Tier-A: pointer to the process-global FfiQueue. The
    # constrained pyo3 dispatcher thread feeds it (one consumer of
    # ``inner.events()``). Audio sources subscribe through it per-frame with a
    # filter narrowing to (capture_audio_frame, source_handle).
    _events: Any = field(default=None, repr=False)
    _proc: Any = field(default=None, repr=False)
    _shutdown_callbacks: list = field(default_factory=list, repr=False)

    def _hangup_on_close(self) -> None:
        # AudioStreamEndpoint.hangup() is fire-and-forget in Rust: the Plivo
        # REST DELETE is spawned on the endpoint's own tokio runtime and the
        # call returns in microseconds. No Python thread (loop or executor) is
        # held for the network round-trip, so calling it inline is safe.
        try:
            self.endpoint.hangup(self.session_id)
        except Exception:
            pass


class AudioStreamServer(AgentServerBase):
    """Plivo audio streaming voice agent server.

    Equivalent of AgentServer but for Plivo WebSocket audio streaming.
    No SIP credentials needed — Plivo connects to your WebSocket server.
    """

    _transport_name = "audio_stream"
    _unit_label = "Session"
    _worker_type = "JT_AUDIO_STREAM"
    _transport_logger_name = "agent_transport.audio_stream"
    _endpoint_not_ready_msg = "Audio stream endpoint not initialized"
    _logger = logger

    def __init__(
        self,
        *,
        listen_addr: str | None = None,
        plivo_auth_id: str | None = None,
        plivo_auth_token: str | None = None,
        sample_rate: int = 8000,
        host: str = "0.0.0.0",
        port: int | None = None,
        agent_id: str | None = None,
        agent_name: str = "audio-stream-agent",
        auth: Callable[..., bool | Coroutine] | None = None,
        recording: bool = True,
        recording_dir: str = "/tmp/agent-sessions",
        recording_stereo: bool = True,
    ) -> None:
        self._listen_addr = listen_addr or os.environ.get("AUDIO_STREAM_ADDR", "0.0.0.0:8765")
        self._plivo_auth_id = plivo_auth_id or os.environ.get("PLIVO_AUTH_ID", "")
        self._plivo_auth_token = plivo_auth_token or os.environ.get("PLIVO_AUTH_TOKEN", "")
        self._sample_rate = sample_rate
        self._active_sessions: dict[str, asyncio.Task] = {}
        self._session_ended_events: dict[str, asyncio.Event] = {}
        self._session_contexts: dict[str, JobContext] = {}
        self._init_common(
            host=host,
            port=port,
            agent_id=agent_id,
            agent_name=agent_name,
            auth=auth,
            recording=recording,
            recording_dir=recording_dir,
            recording_stereo=recording_stereo,
            events=GLOBAL,
        )

    # ── Base hooks ──

    @property
    def _active_map(self) -> dict:
        return self._active_sessions

    @property
    def _contexts(self) -> dict:
        return self._session_contexts

    @property
    def _ended_events(self) -> dict:
        return self._session_ended_events

    def _ffi_global(self):
        return GLOBAL

    def _worker_extra(self) -> dict:
        return {"listen_addr": self._listen_addr}

    def audio_stream_session(self) -> Callable:
        """Decorator to register the session handler."""
        def decorator(fn: Callable[..., Coroutine]) -> Callable:
            self._entrypoint_fnc = fn
            return fn
        return decorator

    def on(self, event_name: str, callback: Callable | None = None) -> Callable:
        """Register a server-level event listener.

        Mirrors :meth:`AgentServer.on` for API symmetry. Plivo's audio_stream
        protocol has no pre-answer phase (Plivo only opens the WebSocket after
        the PSTN call is already up), so there's no ``"ringing"`` event here —
        the hook shape exists for forward compatibility and so user code can
        share handlers between the two server types.
        """
        return super().on(event_name, callback)

    def _on_participant_connected(self, e: FfiEvent, room_handle: str) -> bool:
        info = e.room_event.participant_connected.info
        session_id = info.session_id or room_handle
        plivo_call_uuid = info.identity
        stream_id = info.stream_id
        extra_headers = info.extra_headers or {}
        if session_id in self._active_sessions:
            return False  # duplicate event or retry
        logger.info(
            "Audio stream session %s connected (plivo_call_uuid=%s, stream_id=%s)",
            session_id, plivo_call_uuid, stream_id,
        )
        t = asyncio.create_task(
            self._start_session(session_id, plivo_call_uuid, stream_id, extra_headers)
        )
        self._background_tasks.add(t)
        t.add_done_callback(self._background_tasks.discard)
        return False

    # ── Audio-stream-specific run / orchestration ──

    async def _run(self, *, log_mode: str = "start") -> None:
        self._configure_logging(log_mode)

        if self._entrypoint_fnc is None:
            logger.error(
                "No audio stream session entrypoint registered.\n"
                "Define one using the @server.audio_stream_session() decorator, for example:\n"
                '    @server.audio_stream_session()\n'
                "    async def entrypoint(ctx: JobContext):\n"
                "        ..."
            )
            sys.exit(1)

        loop = asyncio.get_running_loop()

        # Inference executor + user prewarm (shared bootstrap).
        await self._bootstrap_inference_and_setup(loop)

        # Create AudioStreamEndpoint (starts WS server immediately).
        self._ep = AudioStreamEndpoint(
            listen_addr=self._listen_addr,
            plivo_auth_id=self._plivo_auth_id,
            plivo_auth_token=self._plivo_auth_token,
            input_sample_rate=self._sample_rate,
            output_sample_rate=self._sample_rate,
        )
        logger.info("Audio stream WebSocket server on ws://%s", self._listen_addr)

        self._log_observability_status()

        # Wire the constrained pyo3 sink — spawns a dispatcher thread inside
        # Rust that drains ``inner.events()``, translates each event to a
        # LiveKit-shape :class:`FfiEvent` and ``GLOBAL.put``s it on the asyncio
        # loop via ``call_soon_threadsafe``. Replaces the legacy
        # ``run_in_executor(wait_for_event)`` pump, cutting 2-3 asyncio loop
        # ticks per event.
        self._ep.set_event_sink(_on_event_from_rust)

        await self._serve_until_shutdown(loop)

    async def _start_session(self, session_id: str, plivo_call_uuid: str, stream_id: str, extra_headers: dict) -> None:
        session_ended = asyncio.Event()
        self._session_ended_events[session_id] = session_ended

        # Create Room facade BEFORE handler runs — ctx.room is available
        # immediately. remote_kind=0 (STANDARD) because Plivo audio_stream is a
        # WebSocket transport, not SIP — `participant.kind` should reflect that
        # for any agent code that inspects it.
        room = TransportRoom(
            self._ep, session_id,
            agent_name=self._agent_name,
            caller_identity=plivo_call_uuid,
            remote_kind=0,
        )
        ctx = JobContext(
            session_id=session_id,
            plivo_call_uuid=plivo_call_uuid,
            stream_id=stream_id,
            direction="inbound",
            extra_headers=extra_headers,
            endpoint=self._ep,
            userdata=self._userdata,
            _agent_name=self._agent_name,
            _agent_id=self._agent_id,
            _call_ended=session_ended,
            _room=room,
            _proc=self._proc,
            _events=self._events,
        )
        # Make the entrypoint's ctx the canonical job context so
        # get_job_context() is ctx (unifies the dual JobContext, #92).
        _, job_ctx_token = create_transport_context(
            room,
            agent_name=self._agent_name,
            inference_executor=getattr(self, "_inference_executor", None),
            context=ctx,
        )
        ctx._job_ctx_token = job_ctx_token
        self._session_contexts[session_id] = ctx

        async def _run_session():
            # Re-set _JobContextVar in this task's own context so late
            # ``session.close`` listeners find the context regardless of how the
            # close emit is scheduled. The parent context's set() returns a
            # token scoped to the parent — child tasks inherit the value but not
            # always reliably under heavy async churn.
            from livekit.agents.job import _JobContextVar
            _JobContextVar.set(ctx)

            node = _nodename()
            STREAM_SESSIONS_TOTAL.labels(nodename=node).inc()
            session_start = time.monotonic()

            # Start recording if enabled.
            rec_path = None
            rec_started_at = None
            if self._recording:
                try:
                    os.makedirs(self._recording_dir, exist_ok=True)
                    rec_path = os.path.join(self._recording_dir, f"recording_{session_id}.ogg")
                    self._ep.start_recording(session_id, rec_path, self._recording_stereo)
                    rec_started_at = time.time()
                except Exception:
                    rec_path = None
                    logger.warning("Failed to start recording for session %s", session_id, exc_info=True)

            try:
                await self._entrypoint_fnc(ctx)
                # Entrypoint returned — session.start() is non-blocking, so wait
                # for the stream to actually end (Plivo stop or agent shutdown).
                if session_ended and not session_ended.is_set():
                    await session_ended.wait()
            except Exception:
                logger.exception("Session %s handler failed", session_id)
            finally:
                STREAM_SESSION_DURATION.labels(nodename=node).observe(time.monotonic() - session_start)

                await finalize_session(
                    ctx,
                    session_id=session_id,
                    endpoint=self._ep,
                    transport="audio_stream",
                    agent_name=self._agent_name,
                    agent_id=self._agent_id,
                    recording_path=rec_path,
                    recording_started_at=rec_started_at,
                    reason="session ended",
                    logger=logger,
                )
                try:
                    # Fire-and-forget in Rust (Plivo REST DELETE spawned on the
                    # endpoint's tokio runtime) — returns immediately, safe inline.
                    self._ep.hangup(session_id)
                except Exception:
                    pass
                # Cleanup Room facade. We do NOT call
                # ``_JobContextVar.reset(job_ctx_token)`` here: late
                # ``session.close`` listeners can fire AFTER ``session.aclose()``
                # returns and they need ``get_job_context()`` to work. The
                # contextvar is scoped to this asyncio.Task and dies cleanly
                # when the task exits.
                room._on_session_ended()
                self._active_sessions.pop(session_id, None)
                self._session_ended_events.pop(session_id, None)
                self._session_contexts.pop(session_id, None)
                logger.info("Session %s ended", session_id)

        task = asyncio.create_task(_run_session())
        self._active_sessions[session_id] = task
