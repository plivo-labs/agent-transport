"""Shared infrastructure for the LiveKit ``AgentServer`` (SIP) and
``AudioStreamServer`` (Plivo audio streaming).

Both servers are the same machine wearing two transports: identical
inference-executor bootstrap, CPU load monitor, Prometheus/HTTP surface,
FfiQueue lifecycle loop, and force-exit cleanup. The only real
divergence is *how a session is born* (inbound SIP INVITE + outbound
dial vs. an inbound WebSocket) and *how it is hung up* (blocking SIP BYE
off-loop vs. fire-and-forget Plivo REST). Everything else lived as a
byte-for-byte copy in both files until this module pulled it together.

``AgentServerBase`` owns the shared body; subclasses fill in a handful
of hooks:

  * class attributes — ``_transport_name``, ``_unit_label``,
    ``_worker_type``, ``_transport_logger_name``,
    ``_endpoint_not_ready_msg``, ``_logger``
  * ``_active_map`` / ``_contexts`` / ``_ended_events`` — the
    transport-named dicts (kept under their original names,
    ``_active_calls`` vs ``_active_sessions`` etc., so existing tests and
    callers keep working)
  * ``_ffi_global()`` — returns the subclass module's ``GLOBAL`` so the
    test harness can swap it per-module
  * ``_on_participant_connected`` — outbound-aware SIP reservation vs.
    the simpler audio-stream start
  * ``_dispatch_transport_event`` — SIP's pre-answer ``call_ringing``
  * ``_extra_routes`` / ``_worker_extra`` — the ``/call`` route and the
    transport-specific ``/worker`` fields
  * ``_run`` / ``_start_call`` (``_start_session``) — registration and
    per-session orchestration, which genuinely differ

Shutdown model (inherited by both): active sessions are hung up
immediately, ancillary cleanup is bounded by short timeouts, then the
process force-exits via ``os._exit(0)``. ``os._exit`` skips Python
finalization, so anything relying on ``atexit`` or per-session buffered
I/O (recording writes, observability POSTs) must be flushed in the
per-session teardown path, NOT at server shutdown.
"""

import asyncio
import logging
import os
import signal
import sys
import threading

import prometheus_client
from aiohttp import web

from agent_transport import init_logging
from agent_transport._event import FfiEvent
from livekit.agents.inference_runner import _InferenceRunner
from livekit.agents.utils.hw import get_cpu_monitor
from livekit.agents.utils import MovingAverage
from livekit.agents.telemetry.metrics import RUNNING_JOB_GAUGE, CPU_LOAD_GAUGE
from livekit.agents import utils as _lk_utils
from livekit.rtc.room import SipDTMF

from ._audio_io import TransportAudioInput, TransportAudioOutput
from ._aio_utils import call_setup as _call_setup
from ._session_teardown import force_shutdown_agent_session
from .observability import _ensure_transport_tags


class JobProcess:
    """Stub matching LiveKit's JobProcess — holds prewarm data."""
    def __init__(self):
        self.userdata: dict = {}

    @property
    def executor_type(self):
        return None


class JobContextBase:
    """Shared method surface for the SIP / audio-stream public JobContexts.

    The two ``@dataclass`` JobContexts differ only in their transport-
    identifying fields (``remote_uri``/``direction`` vs ``plivo_call_uuid``/
    ``stream_id``) and a few class attributes / the close-hangup strategy.
    Everything else — audio-IO wiring, observability tagging, the per-context
    listener registry — lives here so it can't drift between the two.

    Subclasses set the class attributes below and implement
    ``_hangup_on_close()``. Mixed in BEFORE ``TransportJobContextMixin`` so
    ``proc`` resolves to the JobProcess (not the mixin's self-stub).
    """

    # Override in the dataclass body (plain assignment, NOT annotated — an
    # annotation would turn these into dataclass fields).
    _transport_tag: str = "transport"
    _unit_label: str = "Session"
    _ctx_logger: logging.Logger = logging.getLogger("agent_transport")
    _debug_logger_name: str = "agent_transport"

    @property
    def session(self):
        return self._session

    @property
    def tagger(self):
        # ``_tagger`` is set on ``self`` by ``_init_transport_job_context``.
        return getattr(self, "_tagger", None)

    @property
    def proc(self):
        """Process context — access prewarm data via ctx.proc.userdata."""
        return self._proc

    def set_metadata(self, metadata: dict) -> None:
        """Attach session metadata to native LiveKit observability tags."""
        self.metadata.update({str(key): value for key, value in metadata.items() if value is not None})
        if account_id := self.metadata.get("account_id"):
            self.account_id = str(account_id)
            self.metadata["account_id"] = self.account_id

        _ensure_transport_tags(
            self.tagger,
            account_id=self.account_id,
            transport=self._transport_tag,
            direction=self.direction,
            agent_id=self._agent_id,
            agent_name=self._agent_name,
            metadata=self.metadata,
        )

    @session.setter
    def session(self, session) -> None:
        """Set the agent session — automatically wires transport audio I/O.

        Replaces the manual ctx.start() pattern. After setting ctx.session,
        call session.start(agent=, room=ctx.room) directly.
        """
        self._session = session
        self._primary_agent_session = session

        # Wire transport audio I/O. TransportAudioOutput is a Pattern-A
        # subclass of LiveKit's _ParticipantAudioOutput — buffering /
        # forwarding / interrupt / playout logic are inherited verbatim, with
        # our TransportAudioSource in place of rtc.AudioSource.
        session.input.audio = TransportAudioInput(self.endpoint, self.session_id)
        session.output.audio = TransportAudioOutput(self.endpoint, self.session_id)

        # Listen to session close event — handles agent-initiated shutdown.
        @session.on("close")
        def _on_session_close(ev):
            self._ctx_logger.info(
                "%s %s closed (reason=%s)",
                self._unit_label, self.session_id, getattr(ev, "reason", "unknown"),
            )
            if self._call_ended is not None and not self._call_ended.is_set():
                self._call_ended.set()
            self._hangup_on_close()

        if logging.getLogger(self._debug_logger_name).isEnabledFor(logging.DEBUG):
            @session.on("agent_state_changed")
            def _on_agent_state(ev):
                self._ctx_logger.info("%s %s agent: %s -> %s", self._unit_label, self.session_id, ev.old_state, ev.new_state)
            @session.on("user_state_changed")
            def _on_user_state(ev):
                self._ctx_logger.info("%s %s user: %s -> %s", self._unit_label, self.session_id, ev.old_state, ev.new_state)

    def on(self, event_name: str, callback=None):
        """Register a per-context event listener. Can be used as a decorator."""
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
                self._ctx_logger.exception("Error in %s listener", event_name)

    def _hangup_on_close(self) -> None:
        """Hang up the underlying transport when the session closes.

        SIP schedules a blocking BYE off-loop; audio-stream is fire-and-forget
        and runs inline. Subclass-specific.
        """
        raise NotImplementedError


# ─── Inference executor bootstrap ─────────────────────────────────────────────
# Used only during @setup() so MultilingualModel() etc. work without explicit
# args — the stub is installed before the user's prewarm runs and cleared before
# AgentSession.start() (which is expected to RuntimeError without a real job).

_inference_ctx_token = None


def _set_inference_context(executor) -> None:
    """Temporarily make inference executor available via get_job_context()."""
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
    Returns None if no inference runners are registered.
    """
    from livekit.agents.ipc.inference_proc_executor import InferenceProcExecutor
    import multiprocessing as mp

    runners = _InferenceRunner.registered_runners
    if not runners:
        return None

    return InferenceProcExecutor(
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

    Background thread samples cpu_percent every 0.5s, averaged over a moving
    window of 5 samples (2.5s).
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


class AgentServerBase:
    """Shared body for the SIP / audio-stream LiveKit servers.

    Not instantiated directly — see ``AgentServer`` / ``AudioStreamServer``.
    """

    # ── Subclass-provided identity (override in the class body) ──
    _transport_name: str = "transport"
    _unit_label: str = "Session"            # log noun: "Call" / "Session"
    _worker_type: str = "JT_UNKNOWN"
    _transport_logger_name: str = "agent_transport"
    _endpoint_not_ready_msg: str = "endpoint not initialized"
    _logger: logging.Logger = logging.getLogger("agent_transport.server_base")

    # ── Shared __init__ helper ──
    def _init_common(
        self,
        *,
        host: str,
        port: int | None,
        agent_id: str | None,
        agent_name: str,
        auth,
        recording: bool,
        recording_dir: str,
        recording_stereo: bool,
        events,
    ) -> None:
        """Initialize the fields every server shares.

        Subclass ``__init__`` sets its transport-specific fields (SIP creds /
        listen addr, the transport-named active/ended/context dicts) and calls
        this for the rest.
        """
        self._host = host
        self._port = port or int(os.environ.get("PORT", "8080"))
        # Accept agent_id from kwarg or AGENT_ID env var. Raise on missing
        # rather than substituting a slug — surface the gap loudly at boot
        # instead of corrupting telemetry downstream (obs's agents view keys
        # on it; agent_transport_sessions.agent_id is NOT NULL).
        resolved_agent_id = agent_id or os.environ.get("AGENT_ID") or None
        if not resolved_agent_id:
            raise ValueError(
                f"{type(self).__name__} requires `agent_id` — pass a stable "
                "identifier (typically a UUID4) via the `agent_id=` kwarg or the "
                "AGENT_ID env var. This is the value that keys the obs agents view."
            )
        self._agent_id = resolved_agent_id
        self._agent_name = agent_name
        self._auth = auth
        self._recording = recording
        self._recording_dir = recording_dir
        self._recording_stereo = recording_stereo
        self._entrypoint_fnc = None
        self._setup_fnc = None
        # Set in _bootstrap_inference_and_setup; declared here so the attribute
        # always exists (a SIGINT before _run leaves _run_cleanup reading it).
        self._inference_executor = None
        self._proc = JobProcess()
        self._userdata: dict = {}
        self._ep = None
        # Process-global FfiQueue (LiveKit-faithful mirror of
        # ``FfiClient.instance.queue``). The pyo3 dispatcher thread spawned by
        # ``set_event_sink`` does all the cross-thread ``call_soon_threadsafe``
        # plumbing — events are already on the asyncio loop by the time we read.
        self._events = events
        # Strong-reference set for fire-and-forget asyncio tasks. The event
        # loop only holds weak refs to tasks; without storing them the GC can
        # collect a task mid-execution ("Task was destroyed but it is pending!").
        self._background_tasks: set[asyncio.Task] = set()
        self._load_monitor = _LoadMonitor()
        # Server-level event listeners (distinct from per-JobContext events,
        # which only exist after session creation). Used for pre-answer hooks
        # like ``ringing`` that fire before any JobContext exists.
        self._server_listeners: dict[str, list] = {}

    # ── Hooks the subclass must / may provide ──

    @property
    def _active_map(self) -> dict:
        """The dict of active session/call ids → task (transport-named)."""
        raise NotImplementedError

    @property
    def _contexts(self) -> dict:
        """The dict of session/call id → JobContext (transport-named)."""
        raise NotImplementedError

    @property
    def _ended_events(self) -> dict:
        """The dict of session/call id → asyncio.Event (transport-named)."""
        raise NotImplementedError

    def _ffi_global(self):
        """Return the subclass module's ``GLOBAL`` FfiQueue.

        Read through the subclass so the test harness can swap a fresh queue
        in per-module (``server.GLOBAL = ...`` / ``audio_stream_server.GLOBAL
        = ...``) without the base holding a stale reference.
        """
        raise NotImplementedError

    def _extra_routes(self) -> list:
        """Transport-specific aiohttp routes (SIP adds POST /call)."""
        return []

    def _worker_extra(self) -> dict:
        """Transport-specific fields merged into the /worker response."""
        return {}

    def _dispatch_transport_event(self, sub: str, e: FfiEvent) -> bool:
        """Handle transport_events beyond the shared shutdown/beep set.

        Returns True only to break the lifecycle loop. Default: no-op.
        SIP overrides this for the pre-answer ``call_ringing`` hook.
        """
        return False

    def _on_participant_connected(self, e: FfiEvent, room_handle: str) -> bool:
        """Start a session from a ``participant_connected`` room event."""
        raise NotImplementedError

    # ── Decorators / registration (shared) ──

    @property
    def setup_fnc(self):
        return self._setup_fnc

    @setup_fnc.setter
    def setup_fnc(self, fn):
        """Set prewarm function — fn(proc: JobProcess). Matches LiveKit's server.setup_fnc = prewarm."""
        self._setup_fnc = fn

    def setup(self):
        """Decorator to register a setup function that runs once at startup.

        The function may mutate ``proc.userdata`` (new pattern) or return a
        dict of shared resources (VAD, turn detector, etc.) made available via
        ``ctx.userdata`` in each session.

        Example::
            @server.setup()
            def prewarm():
                return {"vad": silero.VAD.load(), "turn_detector": MultilingualModel()}
        """
        def decorator(fn):
            self._setup_fnc = fn
            return fn
        return decorator

    def on(self, event_name: str, callback=None):
        """Register a server-level event listener.

        Server events fire before a per-session :class:`JobContext` exists, so
        they can't be attached to ``ctx``. Use these for pre-answer hooks like
        call screening, logging, and metrics. Handlers may be ``async def`` or
        plain functions; async handlers are scheduled as background tasks.
        """
        def decorator(fn):
            self._server_listeners.setdefault(event_name, []).append(fn)
            return fn
        if callback is not None:
            return decorator(callback)
        return decorator

    def _emit_server_event(self, event_name: str, *args, **kwargs) -> None:
        """Fire a server-level event to all registered listeners."""
        for listener in self._server_listeners.get(event_name, []):
            try:
                result = listener(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    task = asyncio.create_task(result)
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
            except Exception:
                self._logger.exception("Server event listener for %r failed", event_name)

    # ── CLI ──

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
            """Run in debug mode (DEBUG everything including Rust transport)."""
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
                self._logger.info("Downloading files for %s", plugin.package)
                plugin.download_files()
                self._logger.info("Finished downloading files for %s", plugin.package)

        app()

    # ── Lifecycle / cleanup ──

    async def _serve_until_shutdown(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the HTTP server + lifecycle loop, wait for a signal, then
        run bounded cleanup and force-exit.

        Subclass ``_run`` calls this once the endpoint is registered/created.
        """
        http_app = self._build_http_app()
        runner = web.AppRunner(http_app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port, reuse_address=True)
        await site.start()
        self._logger.info("HTTP server on http://%s:%d", self._host, self._port)

        event_task = asyncio.create_task(self._lifecycle_loop())

        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        await stop.wait()
        try:
            await self._run_cleanup(runner, event_task, loop)
        finally:
            # Flush stdio so the last log lines aren't lost — os._exit skips
            # normal Python finalization.
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
        """Hang up active sessions, drain ancillary resources with short timeouts.

        Split out of the signal-handler path (which adds ``os._exit(0)`` after
        this returns) so tests can exercise the cleanup ordering directly.

        Each step is best-effort: a failing step must not abort the rest —
        partial cleanup beats none, since the Rust endpoint owns background
        threads that can pin the process. Hangups go first and in iteration
        order so callers drop promptly. SIP hangup blocks on a BYE round-trip;
        audio-stream hangup is fire-and-forget. Either way the whole server is
        tearing down, ``ep.shutdown()`` repeats the hangup idempotently, and a
        blocking in-order loop guarantees every hangup is attempted.
        """
        self._logger.info("Shutting down...")
        if self._ep is not None:
            for session_id in list(self._active_map.keys()):
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

    async def _bootstrap_inference_and_setup(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the inference executor and run the user's @setup prewarm.

        Shared boot step: subclass ``_run`` calls this before creating the
        transport endpoint.
        """
        self._inference_executor = _create_inference_executor(loop)
        if self._inference_executor:
            await self._inference_executor.start()
            await self._inference_executor.initialize()
            self._logger.info("Inference executor ready (turn detection models available)")

        if self._setup_fnc:
            if self._inference_executor:
                _set_inference_context(self._inference_executor)
            try:
                await _call_setup(self._setup_fnc, self._proc)
            except Exception:
                self._logger.exception("Setup function failed")
                if self._inference_executor:
                    _clear_inference_context()
                raise
            if self._inference_executor:
                _clear_inference_context()
            self._userdata = self._proc.userdata
            self._logger.info("Setup complete: %s", list(self._userdata.keys()))

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
            logging.getLogger(self._transport_logger_name).setLevel(logging.DEBUG)
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

    # ── HTTP surface ──

    def _build_http_app(self) -> web.Application:
        app = web.Application()
        app.add_routes([
            web.get("/", self._health_handler),
            web.get("/worker", self._worker_handler),
            web.get("/metrics", self._metrics_handler),
            *self._extra_routes(),
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
        node = _nodename()
        CPU_LOAD_GAUGE.labels(nodename=node).set(self._load_monitor.get_load())
        RUNNING_JOB_GAUGE.labels(nodename=node).set(len(self._active_map))

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
            return web.Response(status=503, text=self._endpoint_not_ready_msg)
        return web.Response(text="OK")

    async def _worker_handler(self, request: web.Request) -> web.Response:
        if err := await self._check_auth(request):
            return err
        return web.json_response({
            "agent_name": self._agent_name,
            "worker_type": self._worker_type,
            "worker_load": self._load_monitor.get_load(),
            "active_jobs": len(self._active_map),
            "sdk_version": _get_sdk_version(),
            "project_type": "python",
            **self._worker_extra(),
        })

    # ── FfiQueue lifecycle loop + dispatch ──

    async def _lifecycle_loop(self) -> None:
        """Subscribe to the GLOBAL FfiQueue for lifecycle events.

        The pyo3 dispatcher thread (started by ``set_event_sink``) drains
        ``inner.events()`` on the Rust side, translates each event to an
        :class:`FfiEvent`, and ``GLOBAL.put``s on the asyncio loop via
        ``call_soon_threadsafe``. By the time we ``await q.get()`` the event is
        already on the loop — no ``run_in_executor`` round-trip.
        """
        loop = asyncio.get_running_loop()
        ffi = self._ffi_global()

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

        q = ffi.subscribe(loop=loop, filter_fn=_filter)
        try:
            while True:
                e = await q.get()
                try:
                    if self._dispatch_event(e):
                        break
                except Exception:
                    self._logger.exception(
                        "Error handling %s FfiEvent %r",
                        self._transport_name,
                        e.WhichOneof("message") if e else e,
                    )
                finally:
                    q.task_done()
        except asyncio.CancelledError:
            pass
        finally:
            ffi.unsubscribe(q)

    def _dispatch_event(self, e: FfiEvent) -> bool:
        """Process a single FfiEvent. Returns True if the loop should exit."""
        kind = e.WhichOneof("message")

        if kind == "transport_event":
            sub = e.transport_event.WhichOneof("event")
            if sub == "endpoint_shutdown":
                self._logger.debug("%s lifecycle loop received endpoint_shutdown", self._transport_name)
                return True
            if sub == "beep_detected":
                be = e.transport_event.beep_detected
                session_id = be.source_handle
                self._logger.info(
                    "Beep detected on %s %s (freq=%.0fHz, dur=%dms)",
                    self._unit_label.lower(), session_id, be.frequency_hz, be.duration_ms,
                )
                ctx = self._contexts.get(session_id)
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
                self._logger.debug("Beep timeout on %s %s", self._unit_label.lower(), session_id)
                ctx = self._contexts.get(session_id)
                if ctx:
                    ctx._emit("beep_timeout")
                    if ctx._room:
                        ctx._room.emit("beep_timeout", {})
                return False
            return self._dispatch_transport_event(sub, e)

        if kind == "room_event":
            sub = e.room_event.WhichOneof("participant")
            room_handle = e.room_event.room_handle

            if sub == "participant_connected":
                return self._on_participant_connected(e, room_handle)

            if sub == "participant_disconnected":
                pd = e.room_event.participant_disconnected
                session_id = pd.session_id or room_handle
                self._logger.info("%s %s terminated (reason=%s)", self._unit_label, session_id, pd.reason)

                # Clear audio buffer immediately to abort any pending playout
                # (prevents the 5s "speech not done in time" timeout).
                try:
                    self._ep.clear_buffer(session_id)
                except Exception:
                    pass

                # Synchronously begin tearing down the AgentSession so a
                # buffered STT transcript delivered after disconnect can't
                # trigger a wasted LLM + TTS turn on a dead session (#83). Must
                # run here, on the event-loop wake branch, before we set the
                # ended event — flipping the scheduling guard later (in the
                # _run_* finally) would be too late.
                ctx = self._contexts.get(session_id)
                if ctx is not None and getattr(ctx, "_session", None) is not None:
                    force_shutdown_agent_session(ctx._session, self._background_tasks)

                # Wake the per-session runner (which holds _JobContextVar in
                # its own task context). The participant_disconnected emit MUST
                # run from there — not here — because LiveKit's
                # ``RoomIO._on_participant_disconnected`` synchronously calls
                # ``AgentSession._close_soon`` (asyncio.create_task capturing
                # the current context); emitting from this loop (no
                # JobContextVar) would break ``get_job_context()`` for the
                # close task.
                if session_id in self._ended_events:
                    self._ended_events[session_id].set()
                return False

            if sub == "data_packet_received":
                dp = e.room_event.data_packet_received.value
                if dp and dp.sip_dtmf:
                    session_id = room_handle
                    digit = dp.sip_dtmf.digit
                    if not session_id:
                        self._logger.warning("DTMF event missing session_id, dropping")
                        return False
                    self._logger.debug("DTMF '%s' on %s %s", digit, self._unit_label.lower(), session_id)
                    ctx = self._contexts.get(session_id)
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
