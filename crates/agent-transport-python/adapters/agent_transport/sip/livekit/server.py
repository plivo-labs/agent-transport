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

Shutdown behavior (SIGINT/SIGTERM):
    Active calls are hung up immediately; cleanup (inference executor,
    HTTP server, Rust endpoint) is bounded by short timeouts. The process
    then force-exits via ``os._exit(0)``. This is deliberate — natural exit
    is unreliable because the Rust endpoint owns background threads that
    can pin the process indefinitely.

    Tradeoff: ``os._exit`` skips Python's normal finalization, so any
    resources that rely on ``atexit`` handlers or per-session buffered I/O
    must be flushed in the per-call teardown path (e.g., on the
    ``"call_terminated"`` event), NOT at server shutdown. This includes:
      - recording file writes (flush in session close handler)
      - observability POSTs (await in session close handler)
      - custom ``atexit`` hooks (won't run — migrate to per-session)
"""

import asyncio
import logging
import os
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

import prometheus_client
from aiohttp import web

from agent_transport import SipEndpoint, init_logging
from agent_transport._event import FfiEvent
from agent_transport._event_sink import _on_event_from_rust
from agent_transport._ffi_queue import GLOBAL
from livekit.agents.inference_runner import _InferenceRunner
from livekit.agents.utils.hw import get_cpu_monitor
from livekit.agents.utils import MovingAverage
from livekit.rtc.room import SipDTMF
from ._audio_io import TransportAudioInput, TransportAudioOutput
from ._room_facade import TransportJobContextMixin, TransportRoom, create_transport_context
from ._aio_utils import call_setup as _call_setup
from ._aio_utils import control_executor as _control_executor
from ._aio_utils import schedule_hangup
from ._session_teardown import force_shutdown_agent_session
from ._session_finalize import finalize_session
from .judging import EvaluationConfig
from .observability import _ensure_transport_tags, _get_observability_url

logger = logging.getLogger("agent_transport.server")


class JobProcess:
    """Stub matching LiveKit's JobProcess — holds prewarm data."""
    def __init__(self):
        self.userdata: dict[str, Any] = {}

    @property
    def executor_type(self):
        return None


_inference_ctx_token = None

def _set_inference_context(executor) -> None:
    """Temporarily make inference executor available via get_job_context().
    Used only during @setup() so MultilingualModel() works without explicit args."""
    global _inference_ctx_token
    from livekit.agents.job import _JobContextVar

    class _Stub:
        @property
        def inference_executor(self):
            return executor

    _inference_ctx_token = _JobContextVar.set(_Stub())


def _clear_inference_context() -> None:
    """Remove the temporary stub so AgentSession.start() gets RuntimeError (expected)."""
    global _inference_ctx_token
    if _inference_ctx_token is not None:
        from livekit.agents.job import _JobContextVar
        _JobContextVar.reset(_inference_ctx_token)
        _inference_ctx_token = None


def _create_inference_executor(loop: asyncio.AbstractEventLoop):
    """Create LiveKit's InferenceProcExecutor for local model inference.

    Uses the same subprocess-based executor as LiveKit's AgentServer.
    Models (e.g., turn detection ONNX) run in a separate process for isolation.
    """
    from livekit.agents.ipc.inference_proc_executor import InferenceProcExecutor
    import multiprocessing as mp

    runners = _InferenceRunner.registered_runners
    if not runners:
        return None

    executor = InferenceProcExecutor(
        runners=runners,
        initialize_timeout=5 * 60,
        close_timeout=5,
        memory_warn_mb=2000,
        memory_limit_mb=0,
        ping_interval=5,
        ping_timeout=60,
        high_ping_threshold=2.5,
        mp_ctx=mp.get_context("spawn"),
        loop=loop,
        http_proxy=None,
    )
    return executor

# ─── Prometheus metrics ───────────────────────────────────────────────────────
# Reuse LiveKit's existing gauges (already registered by telemetry/metrics.py)
# and add SIP-specific metrics.

from livekit.agents.telemetry.metrics import RUNNING_JOB_GAUGE, CPU_LOAD_GAUGE
from livekit.agents import utils as _lk_utils

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

def _nodename() -> str:
    return _lk_utils.nodename()


def _get_sdk_version() -> str:
    """Return livekit-agents version — same as what LiveKit reports in /worker."""
    try:
        from livekit.agents.version import __version__
        return __version__
    except ImportError:
        return "unknown"


class _LoadMonitor:
    """CPU load monitor — matches LiveKit's _DefaultLoadCalc exactly.

    Background thread samples cpu_percent every 0.5s, averaged over
    a moving window of 5 samples (2.5s).
    """

    def __init__(self) -> None:
        self._avg = MovingAverage(5)
        self._cpu_monitor = get_cpu_monitor()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()

    def _sample_loop(self) -> None:
        # Cooperative shutdown via _stop event so test harnesses and rapid
        # start/stop cycles don't leak threads. The thread is daemon=True so
        # process exit is unblocked regardless, but a clean stop avoids the
        # 0.5s wasted-CPU window during shutdown.
        while not self._stop.is_set():
            cpu = self._cpu_monitor.cpu_percent(interval=0.5)
            with self._lock:
                self._avg.add_sample(cpu)

    def get_load(self) -> float:
        with self._lock:
            return self._avg.get_avg()

    def stop(self) -> None:
        """Signal the sampler thread to exit and join with a short timeout."""
        self._stop.set()
        # cpu_percent's 0.5s sleep is uninterruptible, so we wait at most
        # ~600ms for it to finish naturally.
        self._thread.join(timeout=1.0)


@dataclass
class JobContext(TransportJobContextMixin):
    """Context passed to the @sip_session handler — equivalent of LiveKit's JobContext.

    Matches LiveKit's standard pattern exactly:
        @server.sip_session()
        async def entrypoint(ctx: JobContext):
            session = AgentSession(vad=..., stt=..., llm=..., tts=...)
            ctx.session = session
            await session.start(agent=Assistant(), room=ctx.room)

    Setting ctx.session automatically wires SIP audio I/O and registers
    the close handler. Then session.start(room=ctx.room) works exactly
    like LiveKit WebRTC.

    DTMF events (equivalent of room.on("sip_dtmf_received") in WebRTC):
        job_ctx = get_job_context()
        job_ctx.room.on("sip_dtmf_received", handler)
    """

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
    # Stable developer-supplied id (typically UUID4). Set by AgentServer
    # when creating the JobContext per call and threaded through to the
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

    @property
    def session(self):
        return self._session

    @property
    def tagger(self):
        # ``_tagger`` is set on ``self`` by ``_init_transport_job_context``.
        return getattr(self, "_tagger", None)

    def set_metadata(self, metadata: dict[str, Any]) -> None:
        """Attach session metadata to native LiveKit observability tags."""
        self.metadata.update({str(key): value for key, value in metadata.items() if value is not None})
        if account_id := self.metadata.get("account_id"):
            self.account_id = str(account_id)
            self.metadata["account_id"] = self.account_id

        _ensure_transport_tags(
            self.tagger,
            account_id=self.account_id,
            transport="sip",
            direction=self.direction,
            agent_id=self._agent_id,
            agent_name=self._agent_name,
            metadata=self.metadata,
        )

    @session.setter
    def session(self, session: Any) -> None:
        """Set the agent session — automatically wires SIP audio I/O.

        This replaces the manual ctx.start() pattern. After setting ctx.session,
        call session.start(agent=, room=ctx.room) directly.
        """
        self._session = session
        self._primary_agent_session = session

        # Wire SIP audio I/O. TransportAudioOutput is a Pattern-A subclass
        # of LiveKit's _ParticipantAudioOutput — buffering / forwarding /
        # interrupt / playout logic are inherited verbatim, with our
        # TransportAudioSource in place of rtc.AudioSource. ``events``
        # defaults to the process-global FfiQueue.
        session.input.audio = TransportAudioInput(self.endpoint, self.session_id)
        session.output.audio = TransportAudioOutput(
            self.endpoint,
            self.session_id,
        )

        # Listen to session close event — handles agent-initiated shutdown
        @session.on("close")
        def _on_session_close(ev):
            logger.info("Call %s session closed (reason=%s)", self.session_id, getattr(ev, 'reason', 'unknown'))
            if self._call_ended is not None and not self._call_ended.is_set():
                self._call_ended.set()
            # hangup() is a Rust block_on (SIP BYE round-trip). This callback
            # fires SYNCHRONOUSLY on the asyncio loop thread, so calling hangup
            # inline stalls every other call on the loop. Schedule it off-loop
            # on the dedicated call-control executor (isolated from the audio
            # forwarders' default pool). ep.shutdown() teardown is the backstop.
            schedule_hangup(self.endpoint.hangup, self.session_id)

        if logging.getLogger("agent_transport.sip").isEnabledFor(logging.DEBUG):
            @session.on("agent_state_changed")
            def _on_agent_state(ev):
                logger.info("Call %s agent: %s -> %s", self.session_id, ev.old_state, ev.new_state)
            @session.on("user_state_changed")
            def _on_user_state(ev):
                logger.info("Call %s user: %s -> %s", self.session_id, ev.old_state, ev.new_state)

    def on(self, event_name: str, callback: Callable | None = None) -> Callable:
        """Register an event listener. Can be used as a decorator."""
        def decorator(fn):
            self._event_listeners.setdefault(event_name, []).append(fn)
            return fn
        if callback is not None:
            return decorator(callback)
        return decorator

    def _emit(self, event_name: str, *args, **kwargs) -> None:
        for listener in self._event_listeners.get(event_name, []):
            try:
                listener(*args, **kwargs)
            except Exception:
                logger.exception("Error in %s listener", event_name)

    @property
    def room(self):
        """Room facade — use with session.start(room=ctx.room) like LiveKit WebRTC."""
        return self._room

    @property
    def proc(self):
        """Process context — access prewarm data via ctx.proc.userdata."""
        return self._proc

    def add_shutdown_callback(self, callback):
        """Register a callback to run when the session ends."""
        super().add_shutdown_callback(callback)


class AgentServer:
    """SIP voice agent server — handles inbound and outbound calls.

    Equivalent of LiveKit's AgentServer.
    """

    def __init__(
        self,
        *,
        sip_server: str | None = None,
        sip_port: int | None = None,
        sip_username: str | None = None,
        sip_password: str | None = None,
        host: str = "0.0.0.0",
        port: int | None = None,
        # `agent_id` is the stable developer-supplied identifier — a
        # UUID4 (or any opaque string) that names this deployment
        # uniquely across accounts. Mandatory: obs's agents view keys
        # on it; agent_transport_sessions.agent_id is NOT NULL.
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
        self._host = host
        self._port = port or int(os.environ.get("PORT", "8080"))
        # Accept agent_id from kwarg or AGENT_ID env var. Raise on missing
        # rather than substituting a slug — see AudioStreamServer for the
        # full rationale; in short: surface the gap loudly at boot time
        # instead of corrupting telemetry downstream.
        resolved_agent_id = agent_id or os.environ.get("AGENT_ID") or None
        if not resolved_agent_id:
            raise ValueError(
                "AgentServer requires `agent_id` — pass a stable identifier "
                "(typically a UUID4) via the `agent_id=` kwarg or the AGENT_ID env "
                "var. This is the value that keys the obs agents view."
            )
        self._agent_id = resolved_agent_id
        self._agent_name = agent_name
        self._auth = auth
        self._recording = recording
        self._recording_dir = recording_dir
        self._recording_stereo = recording_stereo
        self._entrypoint_fnc: Callable[..., Coroutine] | None = None
        self._setup_fnc: Callable | None = None
        self._proc = JobProcess()
        self._userdata: dict[str, Any] = {}
        self._ep: SipEndpoint | None = None
        # Process-global FfiQueue (LiveKit-faithful mirror of
        # ``FfiClient.instance.queue``). The pyo3 dispatcher thread
        # spawned by ``set_event_sink`` does all the cross-thread
        # ``call_soon_threadsafe`` plumbing.
        self._events = GLOBAL
        # Session IDs are strings (returned by Rust CallSession.session_id).
        # The type hints used `int` before — purely cosmetic since Python dict
        # keys are duck-typed, but fix them so mypy/pyright don't scream.
        # Value is None for a slot reserved synchronously by the
        # participant_connected handler (dedup placeholder) and the real Task
        # once _start_call has scheduled _run_call.
        self._active_calls: dict[str, asyncio.Task | None] = {}
        self._call_ended_events: dict[str, asyncio.Event] = {}
        self._call_contexts: dict[str, JobContext] = {}
        # Strong-reference set for fire-and-forget asyncio tasks (outbound
        # dial wrappers, inbound _start_call dispatch). Python's event loop
        # only holds weak references to tasks; without storing them here,
        # the GC can collect a task mid-execution and emit
        # "Task was destroyed but it is pending!" warnings.
        self._background_tasks: set[asyncio.Task] = set()
        # Outbound sessions whose `_start_call` task has been scheduled but
        # may not yet be visible in `_active_calls`. The `call_answered`
        # event handler checks this set to avoid racing against an outbound
        # session that `_start_call` will create asynchronously.
        self._outbound_session_ids: set[str] = set()
        self._load_monitor = _LoadMonitor()
        # Server-level event listeners (distinct from per-JobContext events
        # which only exist after session creation). Used for pre-answer
        # hooks like `ringing` that fire before the JobContext exists.
        self._server_listeners: dict[str, list[Callable]] = {}

    @property
    def setup_fnc(self):
        return self._setup_fnc

    @setup_fnc.setter
    def setup_fnc(self, fn):
        """Set prewarm function — fn(proc: JobProcess). Matches LiveKit's server.setup_fnc = prewarm."""
        self._setup_fnc = fn

    def setup(self) -> Callable:
        """Decorator to register a setup function that runs once at startup.

        The function should return a dict of shared resources (VAD, turn detector, etc.)
        that will be available via ctx.userdata in each call.

        Example::
            @server.setup()
            def prewarm():
                return {"vad": silero.VAD.load(), "turn_detector": MultilingualModel()}
        """
        def decorator(fn: Callable) -> Callable:
            self._setup_fnc = fn
            return fn
        return decorator

    def sip_session(self) -> Callable:
        """Decorator to register the call handler — equivalent of @server.rtc_session()."""
        def decorator(fn: Callable[..., Coroutine]) -> Callable:
            self._entrypoint_fnc = fn
            return fn
        return decorator

    def on(self, event_name: str, callback: Callable | None = None) -> Callable:
        """Register a server-level event listener.

        Server events fire before a per-call :class:`JobContext` exists, so
        they can't be attached to ``ctx``. Use these for pre-answer hooks
        like call screening, logging, and metrics.

        Available events:

        - ``"ringing"`` — fires on inbound SIP calls immediately after Rust
          has sent ``180 Ringing``. Handler receives a ``CallSession`` with
          ``session_id``, ``remote_uri``, ``call_uuid``, and
          ``extra_headers``. Rust auto-answers right after this event, so
          the hook is currently observational only — return values are
          ignored. Future enhancement: allow handlers to return
          ``False``/raise to reject the call before auto-answer runs.

        Handlers may be ``async def`` or plain functions. Async handlers
        are scheduled as background tasks.

        Usage::

            @server.on("ringing")
            def on_ringing(session):
                logger.info("Incoming call from %s", session.remote_uri)

            @server.on("ringing")
            async def log_to_db(session):
                await db.insert_call_record(session.call_uuid)
        """
        def decorator(fn: Callable) -> Callable:
            self._server_listeners.setdefault(event_name, []).append(fn)
            return fn
        if callback is not None:
            return decorator(callback)
        return decorator

    def _emit_server_event(self, event_name: str, *args, **kwargs) -> None:
        """Fire a server-level event to all registered listeners."""
        listeners = self._server_listeners.get(event_name, [])
        for listener in listeners:
            try:
                result = listener(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    task = asyncio.create_task(result)
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
            except Exception:
                logger.exception("Server event listener for %r failed", event_name)

    def run(self, port: int | None = None) -> None:
        """Build CLI and run — equivalent of cli.run_app(server)."""
        if port is not None:
            self._port = port

        try:
            import typer
            from typing import Annotated
        except ImportError:
            asyncio.run(self._run(log_mode="start"))
            return

        app = typer.Typer()

        @app.command()
        def start(
            port: Annotated[int | None, typer.Option(help="HTTP server port", envvar="PORT")] = None,
        ) -> None:
            """Run in production mode (INFO logging)."""
            if port is not None:
                self._port = port
            asyncio.run(self._run(log_mode="start"))

        @app.command()
        def dev(
            port: Annotated[int | None, typer.Option(help="HTTP server port", envvar="PORT")] = None,
        ) -> None:
            """Run in development mode (DEBUG for adapters/pipeline, INFO for Rust)."""
            if port is not None:
                self._port = port
            asyncio.run(self._run(log_mode="dev"))

        @app.command()
        def debug(
            port: Annotated[int | None, typer.Option(help="HTTP server port", envvar="PORT")] = None,
        ) -> None:
            """Run in debug mode (DEBUG everything including Rust SIP/RTP)."""
            if port is not None:
                self._port = port
            asyncio.run(self._run(log_mode="debug"))

        @app.command(name="download-files")
        def download_files() -> None:
            """Download model files for plugins (turn detection, VAD, etc.)."""
            import logging as _logging
            from livekit.agents import Plugin

            _logging.basicConfig(level=_logging.DEBUG)
            for plugin in Plugin.registered_plugins:
                logger.info("Downloading files for %s", plugin.package)
                plugin.download_files()
                logger.info("Finished downloading files for %s", plugin.package)

        app()

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

        # Initialize inference executor for local model inference (turn detection, etc.)
        # Same subprocess approach as LiveKit's AgentServer.
        # We set it on the job context var so MultilingualModel() works transparently —
        # users write the same code as they would with LiveKit's WebRTC transport.
        self._inference_executor = _create_inference_executor(loop)
        if self._inference_executor:
            await self._inference_executor.start()
            await self._inference_executor.initialize()
            logger.info("Inference executor ready (turn detection models available)")

        # Run user's setup function to prewarm models.
        # Temporarily set inference executor on job context so MultilingualModel()
        # works without explicit args — cleared before AgentSession runs.
        if self._setup_fnc:
            if self._inference_executor:
                _set_inference_context(self._inference_executor)
            try:
                await _call_setup(self._setup_fnc, self._proc)
            except Exception:
                logger.exception("Setup function failed")
                if self._inference_executor:
                    _clear_inference_context()
                raise
            if self._inference_executor:
                _clear_inference_context()
            self._userdata = self._proc.userdata
            logger.info("Setup complete: %s", list(self._userdata.keys()))

        self._ep = SipEndpoint(sip_server=self._sip_server)

        # Wire the constrained pyo3 sink. Spawns a dispatcher thread inside
        # Rust that drains ``inner.events()``, translates each event to a
        # LiveKit-shape :class:`FfiEvent` and ``GLOBAL.put``s on the
        # asyncio loop via ``call_soon_threadsafe``. After this call,
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

        # Start HTTP server
        http_app = self._build_http_app()
        runner = web.AppRunner(http_app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port, reuse_address=True)
        await site.start()
        logger.info("HTTP server on http://%s:%d", self._host, self._port)

        # Start lifecycle loop (subscribes to GLOBAL FfiQueue).
        event_task = asyncio.create_task(self._lifecycle_loop())

        # Wait for shutdown signal
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        await stop.wait()
        try:
            await self._run_cleanup(runner, event_task, loop)
        finally:
            # Flush stdio so the last log lines aren't lost —
            # os._exit skips normal Python finalization.
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            os._exit(0)

    async def _run_cleanup(
        self,
        runner: "web.AppRunner",
        event_task: asyncio.Task,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Hang up active calls, drain ancillary resources with short timeouts.

        Split out of ``run()`` so the signal-handler path remains a thin
        wrapper that adds ``os._exit(0)`` after this returns, while tests can
        exercise the cleanup ordering directly.
        """
        logger.info("Shutting down...")
        if self._ep is not None:
            # Hang up every active call up-front, synchronously and in order, so
            # callers drop promptly before the slower cleanup steps below. Per-call
            # hangups during normal operation go off-loop (control_executor) to
            # avoid stalling OTHER live calls on the blocking SIP BYE — but here
            # there are no live calls left to protect: the whole server is tearing
            # down and about to os._exit. A blocking in-order loop is therefore the
            # right choice — it guarantees every BYE is attempted before the
            # process exits. Each call is bounded by the Rust hangup timeout, and
            # ep.shutdown() below repeats the hangup idempotently.
            for session_id in list(self._active_calls.keys()):
                try:
                    self._ep.hangup(session_id)
                except Exception:
                    pass
        event_task.cancel()
        try:
            await asyncio.wait_for(runner.cleanup(), timeout=2.0)
        except Exception:
            pass
        if self._inference_executor:
            try:
                await asyncio.wait_for(self._inference_executor.aclose(), timeout=2.0)
            except Exception:
                pass
        self._load_monitor.stop()
        if self._ep is not None:
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, self._ep.shutdown),
                    timeout=2.0,
                )
            except Exception:
                pass

    def _configure_logging(self, mode: str) -> None:
        if mode == "debug":
            logging.basicConfig(
                level=logging.DEBUG,
                format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
                datefmt="%H:%M:%S",
                force=True,
            )
            init_logging(os.environ.get("RUST_LOG", "debug"))
        elif mode == "dev":
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
                datefmt="%H:%M:%S",
                force=True,
            )
            logging.getLogger("agent_transport.sip").setLevel(logging.DEBUG)
            logging.getLogger("livekit.agents").setLevel(logging.DEBUG)
            logging.getLogger("livekit.plugins").setLevel(logging.DEBUG)
            init_logging(os.environ.get("RUST_LOG", "info"))
        else:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s %(levelname)s %(name)s %(message)s",
                force=True,
            )
            init_logging(os.environ.get("RUST_LOG", "info"))

    def _build_http_app(self) -> web.Application:
        app = web.Application()
        app.add_routes([
            web.get("/", self._health_handler),
            web.get("/worker", self._worker_handler),
            web.get("/metrics", self._metrics_handler),
            web.post("/call", self._call_handler),
        ])
        return app

    async def _check_auth(self, request: web.Request) -> web.Response | None:
        """Returns 401 if auth fails. None if OK or no auth configured."""
        if self._auth is None:
            return None
        result = self._auth(request)
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return None
        return web.json_response({"error": "unauthorized"}, status=401)

    async def _metrics_handler(self, request: web.Request) -> web.Response:
        """Prometheus metrics endpoint — matches LiveKit's /metrics exactly."""
        if err := await self._check_auth(request):
            return err
        loop = asyncio.get_running_loop()
        # Update gauges before scrape
        node = _nodename()
        CPU_LOAD_GAUGE.labels(nodename=node).set(self._load_monitor.get_load())
        RUNNING_JOB_GAUGE.labels(nodename=node).set(len(self._active_calls))

        data = await loop.run_in_executor(None, prometheus_client.generate_latest)
        return web.Response(
            body=data,
            headers={
                "Content-Type": prometheus_client.CONTENT_TYPE_LATEST,
                "Content-Length": str(len(data)),
            },
        )

    async def _health_handler(self, request: web.Request) -> web.Response:
        if not self._ep:
            return web.Response(status=503, text="SIP endpoint not initialized")
        return web.Response(text="OK")

    async def _worker_handler(self, request: web.Request) -> web.Response:
        if err := await self._check_auth(request):
            return err
        return web.json_response({
            "agent_name": self._agent_name,
            "worker_type": "JT_SIP",
            "worker_load": self._load_monitor.get_load(),
            "active_jobs": len(self._active_calls),
            "sdk_version": _get_sdk_version(),
            "project_type": "python",
            "sip_server": self._sip_server,
            "sip_port": self._sip_port,
        })

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

        # Normalize destination for SIP: add sip: prefix and @domain if missing
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
            # Blocking mode: wait for call to connect, then return
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
        else:
            # Non-blocking (default): generate session_id upfront, dial in background
            session_id = "c" + uuid.uuid4().hex[:16]
            # Reserve the session id up-front so call_answered knows it's
            # an outbound call we're driving.
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

    async def _lifecycle_loop(self) -> None:
        """Subscribe to GLOBAL FfiQueue for lifecycle events.

        The pyo3 dispatcher thread (started by ``set_event_sink``) drains
        ``inner.events()`` on the Rust side, translates each event to a
        :class:`FfiEvent`, and ``GLOBAL.put``s on the asyncio loop via
        ``call_soon_threadsafe``. By the time we ``await q.get()`` the
        event is already on the loop — no ``run_in_executor`` round-trip.
        """
        loop = asyncio.get_running_loop()

        def _filter(e: FfiEvent) -> bool:
            kind = e.WhichOneof("message")
            if kind == "room_event":
                return True
            if kind == "transport_event":
                sub = e.transport_event.WhichOneof("event")
                return sub in (
                    "beep_detected", "beep_timeout",
                    "endpoint_shutdown", "call_ringing",
                )
            return False

        q = GLOBAL.subscribe(loop=loop, filter_fn=_filter)
        try:
            while True:
                e = await q.get()
                try:
                    if self._dispatch_event(e):
                        break
                except Exception:
                    logger.exception("Error handling sip FfiEvent %r", e.WhichOneof("message") if e else e)
                finally:
                    q.task_done()
        except asyncio.CancelledError:
            pass
        finally:
            GLOBAL.unsubscribe(q)

    def _dispatch_event(self, e: FfiEvent) -> bool:
        """Process a single FfiEvent. Returns True if the loop should exit."""
        kind = e.WhichOneof("message")

        if kind == "transport_event":
            sub = e.transport_event.WhichOneof("event")
            if sub == "endpoint_shutdown":
                logger.debug("sip lifecycle loop received endpoint_shutdown")
                return True

            if sub == "call_ringing":
                cr = e.transport_event.call_ringing
                logger.info(
                    "Incoming call %s ringing (from=%s, call_uuid=%s)",
                    cr.session_id, cr.remote_uri, cr.call_uuid,
                )
                # Fabricate a minimal session-shaped object so existing
                # ``@server.on("ringing")`` callbacks (which read
                # ``session.session_id``/``remote_uri``/``call_uuid``)
                # keep working without signature changes.
                class _RingingSession:
                    __slots__ = ("session_id", "remote_uri", "call_uuid")
                    def __init__(self_inner):
                        self_inner.session_id = cr.session_id
                        self_inner.remote_uri = cr.remote_uri
                        self_inner.call_uuid = cr.call_uuid
                self._emit_server_event("ringing", _RingingSession())
                return False

            if sub == "beep_detected":
                be = e.transport_event.beep_detected
                session_id = be.source_handle
                logger.info(
                    "Beep detected on call %s (freq=%.0fHz, dur=%dms)",
                    session_id, be.frequency_hz, be.duration_ms,
                )
                ctx = self._call_contexts.get(session_id)
                if ctx:
                    ctx._emit("beep_detected", be.frequency_hz, be.duration_ms)
                    if ctx._room:
                        ctx._room.emit("beep_detected", {
                            "frequency_hz": be.frequency_hz,
                            "duration_ms": be.duration_ms,
                        })
                return False

            if sub == "beep_timeout":
                session_id = e.transport_event.beep_timeout.source_handle
                logger.debug("Beep timeout on call %s", session_id)
                ctx = self._call_contexts.get(session_id)
                if ctx:
                    ctx._emit("beep_timeout")
                    if ctx._room:
                        ctx._room.emit("beep_timeout", {})
                return False
            return False

        if kind == "room_event":
            sub = e.room_event.WhichOneof("participant")
            room_handle = e.room_event.room_handle

            if sub == "participant_connected":
                info = e.room_event.participant_connected.info
                session_id = info.session_id or room_handle
                remote_uri = info.identity

                # Outbound path reserves session_ids synchronously via
                # _outbound_session_ids — skip the participant_connected
                # event for those (HTTP outbound owns session creation).
                if session_id in self._outbound_session_ids:
                    self._outbound_session_ids.discard(session_id)
                    return False
                if session_id in self._active_calls:
                    return False  # duplicate / retry

                # Reserve the slot SYNCHRONOUSLY (before create_task) so a
                # duplicate participant_connected delivered on the very next
                # loop turn — before _start_call's own task has run and set
                # the real task object at the end of _start_call — is deduped
                # here. Mirrors the _outbound_session_ids reservation pattern.
                # _start_call overwrites this None placeholder with the real
                # task; the _run_call finally pops it.
                self._active_calls[session_id] = None
                t = asyncio.create_task(
                    self._start_call(session_id, remote_uri, direction="inbound")
                )
                self._background_tasks.add(t)
                t.add_done_callback(self._background_tasks.discard)
                return False

            if sub == "participant_disconnected":
                pd = e.room_event.participant_disconnected
                session_id = pd.session_id or room_handle
                logger.info("Call %s terminated (reason=%s)", session_id, pd.reason)

                # Clear audio buffer immediately to abort any pending playout
                # (prevents 5s "speech not done in time" timeout).
                try:
                    self._ep.clear_buffer(session_id)
                except Exception:
                    pass

                # Synchronously begin tearing down the AgentSession so a
                # buffered STT transcript delivered after disconnect can't
                # trigger a wasted LLM + TTS turn on a dead call (issue #83).
                # Must run here, on the event-loop wake branch, before we set
                # the call-ended event — flipping the scheduling guard later
                # (in _run_call's finally) would be too late.
                ctx = self._call_contexts.get(session_id)
                if ctx is not None and getattr(ctx, "_session", None) is not None:
                    force_shutdown_agent_session(ctx._session, self._background_tasks)

                # Wake _run_call (which holds _JobContextVar in its own
                # task context). The participant_disconnected emit MUST
                # run from _run_call — not here — because LiveKit's
                # ``RoomIO._on_participant_disconnected`` synchronously
                # calls ``AgentSession._close_soon``, which does
                # ``asyncio.create_task`` and captures the current
                # context. Emitting from this loop (no JobContextVar) would
                # break ``get_job_context()`` for the close task.
                if session_id in self._call_ended_events:
                    self._call_ended_events[session_id].set()
                return False

            if sub == "data_packet_received":
                dp = e.room_event.data_packet_received.value
                if dp and dp.sip_dtmf:
                    session_id = room_handle
                    digit = dp.sip_dtmf.digit
                    if not session_id:
                        logger.warning("DTMF event missing session_id, dropping")
                        return False
                    logger.debug("DTMF '%s' on call %s", digit, session_id)
                    ctx = self._call_contexts.get(session_id)
                    if ctx:
                        ctx._emit("dtmf_received", digit)
                        if ctx._room:
                            dtmf_ev = SipDTMF(
                                code=ord(digit) if digit else 0, digit=digit,
                                participant=ctx._room._remote,
                            )
                            ctx._room.emit("sip_dtmf_received", dtmf_ev)
                return False
            return False

        return False

    async def _start_call(self, session_id: str, remote_uri: str, direction: str) -> None:
        call_ended = asyncio.Event()
        self._call_ended_events[session_id] = call_ended

        # Create Room facade BEFORE handler runs
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
            # ``session.close`` listeners find the context regardless of
            # how the close emit is scheduled. The parent context's
            # set() returns a token scoped to the parent; child tasks
            # inherit the value but not always reliably under heavy
            # async churn.
            from livekit.agents.job import _JobContextVar
            _JobContextVar.set(ctx)

            node = _nodename()
            SIP_CALLS_TOTAL.labels(nodename=node, direction=direction).inc()
            call_start = time.monotonic()

            # Start recording if enabled
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
                # Entrypoint returned — session.start() is non-blocking,
                # so wait for call to actually end (BYE or agent shutdown)
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
                # ``session.close`` listeners (e.g.
                # ``livekit.agents.beta.tools.end_call._on_session_close``)
                # can fire AFTER ``session.aclose()`` returns and they
                # need ``get_job_context()`` to work. The contextvar is
                # scoped to this asyncio.Task and dies cleanly when the
                # task exits.
                room._on_session_ended()
                # No EventWaiter to close. Rust's AudioBuffer::Drop emits
                # audio_capture_error for every pending async_id, which
                # the FfiQueue dispatches and each in-flight
                # capture_frame / wait_for_playout receives via its
                # per-call subscribed Queue. The audio source's finally
                # block does the unsubscribe.
                self._active_calls.pop(session_id, None)
                self._call_ended_events.pop(session_id, None)
                self._call_contexts.pop(session_id, None)
                logger.info("Call %s ended (%s)", session_id, direction)

        task = asyncio.create_task(_run_call())
        self._active_calls[session_id] = task


def run_app(server: AgentServer) -> None:
    """Run the agent server — equivalent of livekit.agents.cli.run_app(server)."""
    try:
        import typer
    except ImportError:
        asyncio.run(server._run(log_mode="start"))
        return

    app = typer.Typer()

    @app.command()
    def start() -> None:
        """Run in production mode (INFO logging)."""
        asyncio.run(server._run(log_mode="start"))

    @app.command()
    def dev() -> None:
        """Run in development mode (DEBUG for adapters/pipeline, INFO for Rust)."""
        asyncio.run(server._run(log_mode="dev"))

    @app.command()
    def debug() -> None:
        """Run in debug mode (DEBUG everything including Rust SIP/RTP)."""
        asyncio.run(server._run(log_mode="debug"))

    @app.command(name="download-files")
    def download_files() -> None:
        """Download model files for plugins (turn detection, VAD, etc.)."""
        import logging as _logging
        from livekit.agents import Plugin

        _logging.basicConfig(level=_logging.DEBUG)
        for plugin in Plugin.registered_plugins:
            logger.info("Downloading files for %s", plugin.package)
            plugin.download_files()
            logger.info("Finished downloading files for %s", plugin.package)

    app()
