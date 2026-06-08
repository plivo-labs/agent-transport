"""AgentServer — drop-in equivalent of LiveKit's AgentServer for SIP transport.

Matches LiveKit's pattern:
    server = AgentServer()

    @server.sip_session()
    async def entrypoint(ctx: JobContext):
        session = AgentSession(vad=..., stt=..., llm=..., tts=...)
        await ctx.start(session, agent=Assistant())

    if __name__ == "__main__":
        run_app(server)

CLI commands (matching LiveKit):
    python agent.py start   — production mode (INFO logging)
    python agent.py dev     — development mode (DEBUG for adapters/pipeline)
    python agent.py debug   — full debug (including Rust SIP/RTP)

Shutdown behavior and the shared server machinery (inference bootstrap,
load monitor, HTTP surface, FfiQueue lifecycle loop, force-exit cleanup)
live in ``_server_base.AgentServerBase``. This file only carries the
SIP-specific pieces: registration, outbound dialing, and the blocking
off-loop hangup.
"""

import asyncio
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

import prometheus_client
from aiohttp import web

from agent_transport import SipEndpoint
from agent_transport._event import FfiEvent
from agent_transport._event_sink import _on_event_from_rust
from agent_transport._ffi_queue import GLOBAL
from ._room_facade import TransportJobContextMixin, TransportRoom, create_transport_context
from ._aio_utils import control_executor as _control_executor
from ._aio_utils import schedule_hangup
from ._session_finalize import finalize_session
from ._server_base import AgentServerBase, JobContextBase, JobProcess, _nodename
from .judging import EvaluationConfig
from .observability import _get_observability_url

logger = logging.getLogger("agent_transport.server")

# ─── SIP-specific Prometheus metrics ──────────────────────────────────────────
# Reuse LiveKit's existing gauges (registered by telemetry/metrics.py); add the
# SIP call counters here.

SIP_CALLS_TOTAL = prometheus_client.Counter(
    "lk_agents_sip_calls_total",
    "Total SIP calls handled",
    ["nodename", "direction"],
)

SIP_CALL_DURATION = prometheus_client.Histogram(
    "lk_agents_sip_call_duration_seconds",
    "SIP call duration in seconds",
    ["nodename"],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600],
)


@dataclass
class JobContext(JobContextBase, TransportJobContextMixin):
    """Context passed to the @sip_session handler — equivalent of LiveKit's JobContext.

    Setting ctx.session automatically wires SIP audio I/O and registers the
    close handler; then ``session.start(room=ctx.room)`` works exactly like
    LiveKit WebRTC. The shared method surface (session wiring, observability
    tagging, listener registry) lives in :class:`JobContextBase`.

    DTMF events (equivalent of room.on("sip_dtmf_received") in WebRTC):
        job_ctx = get_job_context()
        job_ctx.room.on("sip_dtmf_received", handler)
    """

    # JobContextBase hooks (plain class attrs — not dataclass fields).
    _transport_tag = "sip"
    _unit_label = "Call"
    _ctx_logger = logger
    _debug_logger_name = "agent_transport.sip"

    session_id: str
    remote_uri: str
    direction: str  # "inbound" or "outbound"
    endpoint: SipEndpoint
    userdata: dict[str, Any] = field(default_factory=dict)
    extra_headers: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    """Session metadata to attach to native LiveKit observability tags."""
    account_id: str | None = None
    """Account ID for multi-tenancy — set by the consumer per session."""
    evaluation: EvaluationConfig | None = None
    """Post-conversation evaluation config for this session."""

    _agent_name: str = field(default="sip-agent", repr=False)
    # Stable developer-supplied id (typically UUID4). Set by AgentServer when
    # creating the JobContext per call and threaded through to the
    # observability emitter — same convention as AudioStreamServer.
    _agent_id: str = field(default="", repr=False)
    _session: Any = field(default=None, repr=False)
    _call_ended: asyncio.Event | None = field(default=None, repr=False)
    _room: Any = field(default=None, repr=False)
    _job_ctx_token: Any = field(default=None, repr=False)
    _event_listeners: dict = field(default_factory=dict, repr=False)
    _proc: Any = field(default=None, repr=False)
    _shutdown_callbacks: list = field(default_factory=list, repr=False)
    # 0.2.x post-Tier-A: pointer to the process-global FfiQueue. The
    # constrained pyo3 dispatcher thread feeds it (one consumer of
    # ``inner.events()``). Audio sources subscribe through it per-frame.
    _events: Any = field(default=None, repr=False)

    def _hangup_on_close(self) -> None:
        # hangup() is a Rust block_on (SIP BYE round-trip). The close callback
        # fires SYNCHRONOUSLY on the asyncio loop thread, so calling hangup
        # inline stalls every other call on the loop. Schedule it off-loop on
        # the dedicated call-control executor (isolated from the audio
        # forwarders' default pool). ep.shutdown() teardown is the backstop.
        schedule_hangup(self.endpoint.hangup, self.session_id)


class AgentServer(AgentServerBase):
    """SIP voice agent server — handles inbound and outbound calls.

    Equivalent of LiveKit's AgentServer.
    """

    _transport_name = "sip"
    _unit_label = "Call"
    _worker_type = "JT_SIP"
    _transport_logger_name = "agent_transport.sip"
    _endpoint_not_ready_msg = "SIP endpoint not initialized"
    _logger = logger

    def __init__(
        self,
        *,
        sip_server: str | None = None,
        sip_port: int | None = None,
        sip_username: str | None = None,
        sip_password: str | None = None,
        host: str = "0.0.0.0",
        port: int | None = None,
        agent_id: str | None = None,
        agent_name: str = "sip-agent",
        auth: Callable[..., bool | Coroutine] | None = None,
        recording: bool = True,
        recording_dir: str = "/tmp/agent-sessions",
        recording_stereo: bool = True,
    ) -> None:
        self._sip_server = sip_server or os.environ.get("SIP_DOMAIN", "phone.plivo.com")
        self._sip_port = sip_port or int(os.environ.get("SIP_PORT", "5060"))
        self._sip_username = sip_username or os.environ.get("SIP_USERNAME", "")
        self._sip_password = sip_password or os.environ.get("SIP_PASSWORD", "")
        # Value is None for a slot reserved synchronously by the
        # participant_connected handler (dedup placeholder) and the real Task
        # once _start_call has scheduled _run_call.
        self._active_calls: dict[str, asyncio.Task | None] = {}
        self._call_ended_events: dict[str, asyncio.Event] = {}
        self._call_contexts: dict[str, JobContext] = {}
        # Outbound sessions whose `_start_call` task has been scheduled but may
        # not yet be visible in `_active_calls`. The `call_answered` handler
        # checks this set to avoid racing an outbound session that `_start_call`
        # will create asynchronously.
        self._outbound_session_ids: set[str] = set()
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
        return self._active_calls

    @property
    def _contexts(self) -> dict:
        return self._call_contexts

    @property
    def _ended_events(self) -> dict:
        return self._call_ended_events

    def _ffi_global(self):
        return GLOBAL

    def _extra_routes(self) -> list:
        return [web.post("/call", self._call_handler)]

    def _worker_extra(self) -> dict:
        return {"sip_server": self._sip_server, "sip_port": self._sip_port}

    def sip_session(self) -> Callable:
        """Decorator to register the call handler — equivalent of @server.rtc_session()."""
        def decorator(fn: Callable[..., Coroutine]) -> Callable:
            self._entrypoint_fnc = fn
            return fn
        return decorator

    def on(self, event_name: str, callback: Callable | None = None) -> Callable:
        """Register a server-level event listener.

        Server events fire before a per-call :class:`JobContext` exists. The
        SIP transport surfaces ``"ringing"`` — fired on inbound calls right
        after Rust sends ``180 Ringing``, with a session-shaped object
        carrying ``session_id`` / ``remote_uri`` / ``call_uuid``. Rust
        auto-answers immediately after, so the hook is observational only.

        Usage::

            @server.on("ringing")
            def on_ringing(session):
                logger.info("Incoming call from %s", session.remote_uri)
        """
        return super().on(event_name, callback)

    def _dispatch_transport_event(self, sub: str, e: FfiEvent) -> bool:
        if sub == "call_ringing":
            cr = e.transport_event.call_ringing
            logger.info(
                "Incoming call %s ringing (from=%s, call_uuid=%s)",
                cr.session_id, cr.remote_uri, cr.call_uuid,
            )
            self._emit_server_event("ringing", _RingingSession(cr.session_id, cr.remote_uri, cr.call_uuid))
        return False

    def _on_participant_connected(self, e: FfiEvent, room_handle: str) -> bool:
        info = e.room_event.participant_connected.info
        session_id = info.session_id or room_handle
        remote_uri = info.identity

        # Outbound path reserves session_ids synchronously via
        # _outbound_session_ids — skip participant_connected for those (HTTP
        # outbound owns session creation).
        if session_id in self._outbound_session_ids:
            self._outbound_session_ids.discard(session_id)
            return False
        if session_id in self._active_calls:
            return False  # duplicate / retry

        # Reserve the slot SYNCHRONOUSLY (before create_task) so a duplicate
        # participant_connected delivered on the very next loop turn — before
        # _start_call's own task has run and set the real task object — is
        # deduped here. _start_call overwrites this None placeholder with the
        # real task; the _run_call finally pops it.
        self._active_calls[session_id] = None
        t = asyncio.create_task(
            self._start_call(session_id, remote_uri, direction="inbound")
        )
        self._background_tasks.add(t)
        t.add_done_callback(self._background_tasks.discard)
        return False

    # ── SIP-specific run / dialing / orchestration ──

    async def _call_handler(self, request: web.Request) -> web.Response:
        if err := await self._check_auth(request):
            return err
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        raw_to = data.get("to", "")
        if not raw_to:
            return web.json_response({"error": "missing 'to' field"}, status=400)

        # Normalize destination for SIP: add sip: prefix and @domain if missing.
        destination = raw_to
        if not destination.startswith("sip:"):
            destination = "sip:" + destination
        if "@" not in destination.split(":", 1)[1]:
            destination = destination + "@" + self._sip_server

        from_uri = data.get("from")  # Optional SIP From URI
        raw_from = from_uri or ""
        headers = data.get("headers")  # Optional custom SIP headers
        wait = data.get("wait_until_answered", False)

        loop = asyncio.get_running_loop()

        if wait:
            # Blocking mode: wait for the call to connect, then return.
            try:
                session_id = await loop.run_in_executor(
                    None, lambda: self._ep.call(destination, from_uri, headers)
                )
            except Exception as e:
                return web.json_response({"error": str(e)}, status=500)

            logger.info("Outbound call %s to %s connected (from=%s)", session_id, destination, from_uri or "default")
            # Mark synchronously so the event loop doesn't race-create a
            # duplicate session when `call_answered` arrives.
            self._outbound_session_ids.add(session_id)
            t = asyncio.create_task(self._start_call(session_id, destination, direction="outbound"))
            self._background_tasks.add(t)
            t.add_done_callback(self._background_tasks.discard)
            return web.json_response({
                "session_id": session_id, "status": "connected",
                "to": raw_to, "from": raw_from,
            })

        # Non-blocking (default): generate session_id upfront, dial in background.
        session_id = "c" + uuid.uuid4().hex[:16]
        # Reserve the session id up-front so call_answered knows it's an
        # outbound call we're driving.
        self._outbound_session_ids.add(session_id)

        async def _dial():
            try:
                returned_id = await loop.run_in_executor(
                    None, lambda: self._ep.call(destination, from_uri, headers, session_id)
                )
                logger.info("Outbound call %s to %s connected (from=%s)", returned_id, destination, from_uri or "default")
                await self._start_call(returned_id, destination, direction="outbound")
            except Exception as e:
                logger.warning("Outbound call %s to %s failed: %s", session_id, destination, e)
                self._outbound_session_ids.discard(session_id)

        t = asyncio.create_task(_dial())
        self._background_tasks.add(t)
        t.add_done_callback(self._background_tasks.discard)
        return web.json_response({
            "session_id": session_id, "status": "dialing",
            "to": raw_to, "from": raw_from,
        })

    async def _run(self, *, log_mode: str = "start") -> None:
        self._configure_logging(log_mode)

        if not self._sip_username or not self._sip_password:
            logger.error("Set SIP_USERNAME and SIP_PASSWORD environment variables")
            sys.exit(1)

        if self._entrypoint_fnc is None:
            logger.error(
                "No SIP session entrypoint registered.\n"
                "Define one using the @server.sip_session() decorator, for example:\n"
                '    @server.sip_session()\n'
                "    async def entrypoint(ctx: JobContext):\n"
                "        ..."
            )
            sys.exit(1)

        loop = asyncio.get_running_loop()

        # Inference executor + user prewarm (shared bootstrap).
        await self._bootstrap_inference_and_setup(loop)

        self._ep = SipEndpoint(sip_server=self._sip_server)

        # Wire the constrained pyo3 sink. Spawns a dispatcher thread inside Rust
        # that drains ``inner.events()``, translates each event to a
        # LiveKit-shape :class:`FfiEvent` and ``GLOBAL.put``s on the asyncio
        # loop via ``call_soon_threadsafe``. After this call,
        # ``wait_for_event``/``poll_event`` return None — all events flow
        # through GLOBAL.
        self._ep.set_event_sink(_on_event_from_rust)

        # Subscribe BEFORE register so we never miss the ``endpoint_registered``
        # transport_event (subscribe-before-request pattern).
        reg_q = GLOBAL.subscribe(
            loop=loop,
            filter_fn=lambda e: (
                e.WhichOneof("message") == "transport_event"
                and e.transport_event.WhichOneof("event") == "endpoint_registered"
            ),
        )
        try:
            await loop.run_in_executor(
                None, self._ep.register, self._sip_username, self._sip_password
            )
            try:
                await asyncio.wait_for(reg_q.get(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.error("SIP registration timed out after 10s")
                sys.exit(1)
        finally:
            GLOBAL.unsubscribe(reg_q)

        logger.info("Registered as %s@%s:%d", self._sip_username, self._sip_server, self._sip_port)

        obs_url = _get_observability_url()
        if obs_url:
            logger.info("Observability enabled, target %s", obs_url)

        await self._serve_until_shutdown(loop)

    async def _start_call(self, session_id: str, remote_uri: str, direction: str) -> None:
        call_ended = asyncio.Event()
        self._call_ended_events[session_id] = call_ended

        # Create Room facade BEFORE handler runs.
        room = TransportRoom(
            self._ep, session_id,
            agent_name=self._agent_name,
            caller_identity=remote_uri,
        )
        ctx = JobContext(
            session_id=session_id,
            remote_uri=remote_uri,
            direction=direction,
            endpoint=self._ep,
            userdata=self._userdata,
            _agent_name=self._agent_name,
            _agent_id=self._agent_id,
            _call_ended=call_ended,
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
        self._call_contexts[session_id] = ctx

        async def _run_call():
            # Re-set _JobContextVar in this task's own context so late
            # ``session.close`` listeners find the context regardless of how the
            # close emit is scheduled. The parent context's set() returns a
            # token scoped to the parent; child tasks inherit the value but not
            # always reliably under heavy async churn.
            from livekit.agents.job import _JobContextVar
            _JobContextVar.set(ctx)

            node = _nodename()
            SIP_CALLS_TOTAL.labels(nodename=node, direction=direction).inc()
            call_start = time.monotonic()

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
                    logger.warning("Failed to start recording for call %s", session_id, exc_info=True)

            try:
                await self._entrypoint_fnc(ctx)
                # Entrypoint returned — session.start() is non-blocking, so wait
                # for the call to actually end (BYE or agent shutdown).
                if call_ended and not call_ended.is_set():
                    await call_ended.wait()
            except Exception:
                logger.exception("Call %s handler failed", session_id)
            finally:
                SIP_CALL_DURATION.labels(nodename=node).observe(time.monotonic() - call_start)
                logger.info("Call %s cleanup: session=%s", session_id, "set" if ctx._session is not None else "None")

                await finalize_session(
                    ctx,
                    session_id=session_id,
                    endpoint=self._ep,
                    transport="sip",
                    agent_name=self._agent_name,
                    agent_id=self._agent_id,
                    recording_path=rec_path,
                    recording_started_at=rec_started_at,
                    reason="call ended",
                    logger=logger,
                )
                try:
                    # Off-loop on the dedicated call-control executor: hangup is
                    # a blocking SIP BYE; awaiting it there keeps the loop free
                    # for other calls' teardown without contending for the audio
                    # forwarders' default pool.
                    await asyncio.get_running_loop().run_in_executor(
                        _control_executor(), self._ep.hangup, session_id
                    )
                except Exception:
                    pass
                # Cleanup Room facade. We do NOT call
                # ``_JobContextVar.reset(job_ctx_token)`` here: late
                # ``session.close`` listeners can fire AFTER ``session.aclose()``
                # returns and they need ``get_job_context()`` to work. The
                # contextvar is scoped to this asyncio.Task and dies cleanly
                # when the task exits.
                room._on_session_ended()
                self._active_calls.pop(session_id, None)
                self._call_ended_events.pop(session_id, None)
                self._call_contexts.pop(session_id, None)
                logger.info("Call %s ended (%s)", session_id, direction)

        task = asyncio.create_task(_run_call())
        self._active_calls[session_id] = task


class _RingingSession:
    """Minimal session-shaped object for ``@server.on("ringing")`` callbacks.

    Carries just the fields existing handlers read (``session_id`` /
    ``remote_uri`` / ``call_uuid``) so they keep working without signature
    changes.
    """
    __slots__ = ("session_id", "remote_uri", "call_uuid")

    def __init__(self, session_id: str, remote_uri: str, call_uuid: str):
        self.session_id = session_id
        self.remote_uri = remote_uri
        self.call_uuid = call_uuid


def run_app(server: AgentServer) -> None:
    """Run the agent server — equivalent of livekit.agents.cli.run_app(server)."""
    server.run()
