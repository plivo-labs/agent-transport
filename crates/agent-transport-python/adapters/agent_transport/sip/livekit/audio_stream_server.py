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

CLI commands (matching LiveKit):
    python agent.py start   — production mode (INFO logging)
    python agent.py dev     — development mode (DEBUG for adapters/pipeline)
    python agent.py debug   — full debug (including Rust transport)
"""

import asyncio
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

import prometheus_client
from aiohttp import web

from agent_transport import AudioStreamEndpoint, init_logging
from agent_transport._event import FfiEvent
from agent_transport._event_sink import _on_event_from_rust
from agent_transport._ffi_queue import GLOBAL
from livekit.agents.inference_runner import _InferenceRunner
from livekit.agents.utils.hw import get_cpu_monitor
from livekit.agents.utils import MovingAverage
from ._audio_io import TransportAudioInput, TransportAudioOutput
from ._room_facade import TransportJobContextMixin, TransportRoom, create_transport_context
from ._aio_utils import call_setup as _call_setup, close_session_services
from livekit.rtc.room import SipDTMF
from .server import JobProcess

logger = logging.getLogger("agent_transport.audio_stream_server")


# ─── Shared helpers (reuse from server.py) ────────────────────────────────────

_inference_ctx_token = None

def _set_inference_context(executor) -> None:
    global _inference_ctx_token
    from livekit.agents.job import _JobContextVar

    class _Stub:
        @property
        def inference_executor(self):
            return executor

    _inference_ctx_token = _JobContextVar.set(_Stub())


def _clear_inference_context() -> None:
    global _inference_ctx_token
    if _inference_ctx_token is not None:
        from livekit.agents.job import _JobContextVar
        _JobContextVar.reset(_inference_ctx_token)
        _inference_ctx_token = None


def _create_inference_executor(loop: asyncio.AbstractEventLoop):
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

from livekit.agents.telemetry.metrics import RUNNING_JOB_GAUGE, CPU_LOAD_GAUGE
from livekit.agents import utils as _lk_utils

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

def _nodename() -> str:
    return _lk_utils.nodename()


def _get_sdk_version() -> str:
    try:
        from livekit.agents.version import __version__
        return __version__
    except ImportError:
        return "unknown"


class _LoadMonitor:
    def __init__(self) -> None:
        self._avg = MovingAverage(5)
        self._cpu_monitor = get_cpu_monitor()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()

    def _sample_loop(self) -> None:
        # Cooperative shutdown via _stop event so the thread doesn't keep
        # sampling CPU during process teardown. daemon=True still guarantees
        # process exit, but a clean stop is friendlier to test harnesses.
        while not self._stop.is_set():
            cpu = self._cpu_monitor.cpu_percent(interval=0.5)
            with self._lock:
                self._avg.add_sample(cpu)

    def get_load(self) -> float:
        with self._lock:
            return self._avg.get_avg()

    def stop(self) -> None:
        """Signal the sampler thread to exit and join briefly."""
        self._stop.set()
        self._thread.join(timeout=1.0)


# ─── JobContext ───────────────────────────────────────────────────

@dataclass
class JobContext(TransportJobContextMixin):
    """Context passed to the @audio_stream_session handler.

    Matches LiveKit's standard pattern exactly:
        @server.audio_stream_session()
        async def entrypoint(ctx: JobContext):
            session = AgentSession(vad=..., stt=..., llm=..., tts=...)
            ctx.session = session
            await session.start(agent=Assistant(), room=ctx.room)

    Setting ctx.session automatically wires audio stream I/O and registers
    the close handler. Then session.start(room=ctx.room) works exactly
    like LiveKit WebRTC.

    DTMF events (equivalent of room.on("sip_dtmf_received") in WebRTC):
        job_ctx = get_job_context()
        job_ctx.room.on("sip_dtmf_received", handler)
    """

    session_id: str
    plivo_call_uuid: str      # Plivo Call UUID
    stream_id: str            # Plivo Stream UUID
    direction: str            # Always "inbound" for audio streams
    extra_headers: dict[str, str]
    endpoint: AudioStreamEndpoint
    userdata: dict[str, Any] = field(default_factory=dict)

    _agent_name: str = field(default="agent", repr=False)
    _session: Any = field(default=None, repr=False)
    _call_ended: asyncio.Event | None = field(default=None, repr=False)
    _room: Any = field(default=None, repr=False)
    _job_stub: Any = field(default=None, repr=False)
    _job_ctx_token: Any = field(default=None, repr=False)
    _event_listeners: dict = field(default_factory=dict, repr=False)
    # 0.2.x post-Tier-A: pointer to the process-global FfiQueue. The
    # constrained pyo3 dispatcher thread feeds it (one consumer of
    # ``inner.events()``). Audio sources subscribe through it per-frame
    # with a filter narrowing to (capture_audio_frame, source_handle).
    _events: Any = field(default=None, repr=False)
    _proc: Any = field(default=None, repr=False)
    _shutdown_callbacks: list = field(default_factory=list, repr=False)

    @property
    def session(self):
        return self._session

    @session.setter
    def session(self, session: Any) -> None:
        """Set the agent session — automatically wires audio stream I/O.

        This replaces the manual ctx.start() pattern. After setting ctx.session,
        call session.start(agent=, room=ctx.room) directly.
        """
        self._session = session

        # Wire audio stream I/O. TransportAudioOutput is a Pattern-A
        # subclass of LiveKit's _ParticipantAudioOutput — buffering /
        # forwarding / interrupt / playout logic are inherited verbatim,
        # with our TransportAudioSource in place of rtc.AudioSource.
        # ``events`` defaults to the process-global FfiQueue inside
        # TransportAudioSource.
        session.input.audio = TransportAudioInput(self.endpoint, self.session_id)
        session.output.audio = TransportAudioOutput(
            self.endpoint,
            self.session_id,
        )

        # Listen to session close event — handles agent-initiated shutdown
        @session.on("close")
        def _on_session_close(ev):
            logger.info("Session %s closed (reason=%s)", self.session_id, getattr(ev, 'reason', 'unknown'))
            if self._call_ended is not None and not self._call_ended.is_set():
                self._call_ended.set()
            # AudioStreamEndpoint.hangup() is fire-and-forget in Rust: the Plivo
            # REST DELETE is spawned on the endpoint's own tokio runtime and the
            # call returns in microseconds. No Python thread (loop or executor)
            # is held for the network round-trip, so calling it inline here does
            # not stall the loop.
            try:
                self.endpoint.hangup(self.session_id)
            except Exception:
                pass

        if logging.getLogger("agent_transport.audio_stream").isEnabledFor(logging.DEBUG):
            @session.on("agent_state_changed")
            def _on_agent_state(ev):
                logger.info("Session %s agent: %s -> %s", self.session_id, ev.old_state, ev.new_state)
            @session.on("user_state_changed")
            def _on_user_state(ev):
                logger.info("Session %s user: %s -> %s", self.session_id, ev.old_state, ev.new_state)

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


# ─── AudioStreamServer ───────────────────────────────────────────────────────

class AudioStreamServer:
    """Plivo audio streaming voice agent server.

    Equivalent of AgentServer but for Plivo WebSocket audio streaming.
    No SIP credentials needed — Plivo connects to your WebSocket server.
    """

    def __init__(
        self,
        *,
        listen_addr: str | None = None,
        plivo_auth_id: str | None = None,
        plivo_auth_token: str | None = None,
        sample_rate: int = 8000,
        host: str = "0.0.0.0",
        port: int | None = None,
        agent_name: str = "audio-stream-agent",
        auth: Callable[..., bool | Coroutine] | None = None,
    ) -> None:
        # Process-global FfiQueue (LiveKit-faithful mirror of
        # ``FfiClient.instance.queue``). The pyo3 dispatcher thread
        # spawned by ``set_event_sink`` does all the cross-thread
        # ``call_soon_threadsafe`` plumbing — by the time events land
        # here they're already on the asyncio loop.
        self._events = GLOBAL
        self._listen_addr = listen_addr or os.environ.get("AUDIO_STREAM_ADDR", "0.0.0.0:8765")
        self._plivo_auth_id = plivo_auth_id or os.environ.get("PLIVO_AUTH_ID", "")
        self._plivo_auth_token = plivo_auth_token or os.environ.get("PLIVO_AUTH_TOKEN", "")
        self._sample_rate = sample_rate
        self._host = host
        self._port = port or int(os.environ.get("PORT", "8080"))
        self._agent_name = agent_name
        self._auth = auth
        self._entrypoint_fnc: Callable[..., Coroutine] | None = None
        self._setup_fnc: Callable | None = None
        self._proc = JobProcess()
        self._userdata: dict[str, Any] = {}
        self._ep: AudioStreamEndpoint | None = None
        # Session IDs are strings (Rust CallSession.session_id). Type hints
        # were `int` previously — duck-typed at runtime, fixing the
        # annotations so mypy/pyright agree with reality.
        self._active_sessions: dict[str, asyncio.Task] = {}
        self._session_ended_events: dict[str, asyncio.Event] = {}
        self._session_contexts: dict[str, JobContext] = {}
        # Strong-reference set for fire-and-forget asyncio tasks (session
        # start dispatch). Python's event loop only holds weak references
        # to tasks; without storing them here, the GC can collect a task
        # mid-execution and emit "Task was destroyed but it is pending!"
        # warnings.
        self._background_tasks: set[asyncio.Task] = set()
        self._load_monitor = _LoadMonitor()
        # Server-level event listeners. Plivo's audio_stream protocol has
        # no pre-answer phase, so `ringing` doesn't fire here — but we
        # expose the same `on(event_name)` shape as AgentServer for API
        # symmetry. Future events (e.g., "session_start") can be added.
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

        Example::
            @server.setup()
            def prewarm():
                return {"vad": silero.VAD.load(), "turn_detector": MultilingualModel()}
        """
        def decorator(fn: Callable) -> Callable:
            self._setup_fnc = fn
            return fn
        return decorator

    def on(self, event_name: str, callback: Callable | None = None) -> Callable:
        """Register a server-level event listener.

        Mirrors :meth:`AgentServer.on` for API symmetry. Plivo's
        audio_stream protocol doesn't surface a pre-answer ringing phase
        — Plivo only opens the WebSocket after the PSTN call is already
        up — so there's no ``"ringing"`` event on this transport. The
        hook shape exists for forward compatibility and so user code can
        share handlers between the two server types.
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

    def audio_stream_session(self) -> Callable:
        """Decorator to register the session handler."""
        def decorator(fn: Callable[..., Coroutine]) -> Callable:
            self._entrypoint_fnc = fn
            return fn
        return decorator

    def run(self, port: int | None = None) -> None:
        """Build CLI and run."""
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
                logger.info("Downloading files for %s", plugin.package)
                plugin.download_files()
                logger.info("Finished downloading files for %s", plugin.package)

        app()

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

        # Initialize inference executor
        self._inference_executor = _create_inference_executor(loop)
        if self._inference_executor:
            await self._inference_executor.start()
            await self._inference_executor.initialize()
            logger.info("Inference executor ready (turn detection models available)")

        # Run user's setup function (supports sync and async)
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

        # Create AudioStreamEndpoint (starts WS server immediately)
        self._ep = AudioStreamEndpoint(
            listen_addr=self._listen_addr,
            plivo_auth_id=self._plivo_auth_id,
            plivo_auth_token=self._plivo_auth_token,
            input_sample_rate=self._sample_rate,
            output_sample_rate=self._sample_rate,
        )
        logger.info("Audio stream WebSocket server on ws://%s", self._listen_addr)

        # Wire the constrained pyo3 sink — spawns a dispatcher thread inside
        # Rust that drains ``inner.events()``, translates each event to a
        # LiveKit-shape :class:`FfiEvent` and ``GLOBAL.put``s it on the
        # asyncio loop via ``call_soon_threadsafe``. Replaces the legacy
        # ``run_in_executor(wait_for_event)`` pump, cutting 2-3 asyncio
        # loop ticks per event.
        self._ep.set_event_sink(_on_event_from_rust)

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
        logger.info("Shutting down...")
        event_task.cancel()

        if self._active_sessions:
            logger.info("Draining %d active session(s)...", len(self._active_sessions))
            await asyncio.gather(*self._active_sessions.values(), return_exceptions=True)

        await runner.cleanup()
        if self._inference_executor:
            await self._inference_executor.aclose()
        self._load_monitor.stop()
        # ep.shutdown() does block_on for cancel + per-session hangup. Wrap
        # in run_in_executor so the asyncio loop isn't blocked during teardown.
        await loop.run_in_executor(None, self._ep.shutdown)

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
            logging.getLogger("agent_transport.audio_stream").setLevel(logging.DEBUG)
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
        ])
        return app

    async def _check_auth(self, request: web.Request) -> web.Response | None:
        if self._auth is None:
            return None
        result = self._auth(request)
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return None
        return web.json_response({"error": "unauthorized"}, status=401)

    async def _metrics_handler(self, request: web.Request) -> web.Response:
        if err := await self._check_auth(request):
            return err
        loop = asyncio.get_running_loop()
        node = _nodename()
        CPU_LOAD_GAUGE.labels(nodename=node).set(self._load_monitor.get_load())
        RUNNING_JOB_GAUGE.labels(nodename=node).set(len(self._active_sessions))

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
            return web.Response(status=503, text="Audio stream endpoint not initialized")
        return web.Response(text="OK")

    async def _worker_handler(self, request: web.Request) -> web.Response:
        if err := await self._check_auth(request):
            return err
        return web.json_response({
            "agent_name": self._agent_name,
            "worker_type": "JT_AUDIO_STREAM",
            "worker_load": self._load_monitor.get_load(),
            "active_jobs": len(self._active_sessions),
            "sdk_version": _get_sdk_version(),
            "project_type": "python",
            "listen_addr": self._listen_addr,
        })

    async def _lifecycle_loop(self) -> None:
        """Subscribe to GLOBAL FfiQueue for lifecycle events.

        The pyo3 dispatcher thread (started by ``set_event_sink``) drains
        ``inner.events()`` on the Rust side, translates each event to a
        :class:`FfiEvent`, and ``GLOBAL.put``s on the asyncio loop via
        ``call_soon_threadsafe``. By the time we ``await q.get()`` the
        event is already on the loop — no ``run_in_executor`` round-trip,
        so 2-3 asyncio ticks are saved per event vs. the previous
        wait_for_event-based pump.
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
                    logger.exception("Error handling audio_stream FfiEvent %r", e.WhichOneof("message") if e else e)
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
                logger.debug("audio_stream lifecycle loop received endpoint_shutdown")
                return True
            if sub == "beep_detected":
                be = e.transport_event.beep_detected
                session_id = be.source_handle
                logger.info(
                    "Beep detected on session %s (freq=%.0fHz, dur=%dms)",
                    session_id, be.frequency_hz, be.duration_ms,
                )
                ctx = self._session_contexts.get(session_id)
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
                logger.debug("Beep timeout on session %s", session_id)
                ctx = self._session_contexts.get(session_id)
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

            if sub == "participant_disconnected":
                pd = e.room_event.participant_disconnected
                session_id = pd.session_id or room_handle
                logger.info("Session %s terminated (reason=%s)", session_id, pd.reason)
                try:
                    self._ep.clear_buffer(session_id)
                except Exception:
                    pass
                # Wake _run_session (which holds _JobContextVar). The
                # ``participant_disconnected`` emit must come from
                # _run_session — not here — because LiveKit's RoomIO
                # synchronously calls ``AgentSession._close_soon`` which
                # captures the current task's context.
                if session_id in self._session_ended_events:
                    self._session_ended_events[session_id].set()
                return False

            if sub == "data_packet_received":
                dp = e.room_event.data_packet_received.value
                if dp and dp.sip_dtmf:
                    session_id = room_handle
                    digit = dp.sip_dtmf.digit
                    logger.debug("DTMF '%s' on session %s", digit, session_id)
                    ctx = self._session_contexts.get(session_id)
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

    async def _start_session(self, session_id: str, plivo_call_uuid: str, stream_id: str, extra_headers: dict) -> None:
        session_ended = asyncio.Event()
        self._session_ended_events[session_id] = session_ended

        # Create Room facade BEFORE handler runs — ctx.room is available immediately.
        # remote_kind=0 (STANDARD) because Plivo audio_stream is a WebSocket
        # transport, not SIP — `participant.kind` should reflect that for any
        # agent code that inspects it.
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
            # ``session.close`` listeners (e.g.
            # ``livekit.agents.beta.tools.end_call._on_session_close``)
            # find the context regardless of how the close emit is
            # scheduled. The parent context's set() returns a token
            # scoped to the parent — child tasks inherit the value but
            # not always reliably under heavy async churn.
            from livekit.agents.job import _JobContextVar
            _JobContextVar.set(ctx)

            node = _nodename()
            STREAM_SESSIONS_TOTAL.labels(nodename=node).inc()
            session_start = time.monotonic()

            try:
                await self._entrypoint_fnc(ctx)
                # Entrypoint returned — session.start() is non-blocking,
                # so wait for stream to actually end (Plivo stop or agent shutdown)
                if session_ended and not session_ended.is_set():
                    await session_ended.wait()
            except Exception:
                logger.exception("Session %s handler failed", session_id)
            finally:
                STREAM_SESSION_DURATION.labels(nodename=node).observe(time.monotonic() - session_start)

                # Emit ``participant_disconnected`` from THIS task's
                # context (which has ``_JobContextVar`` set). LiveKit's
                # RoomIO listener synchronously calls
                # ``AgentSession._close_soon`` →
                # ``asyncio.create_task(_aclose_impl(...))``. The new
                # task inherits the CURRENT context, so doing the emit
                # here ensures the close task has the JobContextVar set
                # and ``end_call.py:_on_session_close`` can call
                # ``get_job_context()`` successfully.
                if ctx and ctx._room and getattr(ctx._room, "_remote", None):
                    try:
                        remote = ctx._room._remote
                        remote.disconnect_reason = 1  # CLIENT_INITIATED
                        ctx._room.emit("participant_disconnected", remote)
                    except Exception:
                        logger.exception("participant_disconnected emit failed")

                if ctx._session is not None:
                    try:
                        usage = ctx._session.usage
                        if usage and usage.model_usage:
                            logger.info("Session %s usage: %s", session_id, usage)
                    except Exception:
                        pass
                    try:
                        await ctx._session.aclose()
                    except Exception:
                        pass
                    # AgentSession.aclose() does NOT cascade-close the
                    # user-supplied STT/TTS/LLM (verified against
                    # livekit-agents 1.5.15), so their vendor WebSockets
                    # would leak per session on our long-lived in-process
                    # server. Close them explicitly after the session has
                    # drained, before shutdown callbacks / hangup. Never
                    # raises, so it cannot skip the steps below.
                    await close_session_services(ctx._session, logger=logger)
                # Fire shutdown callbacks once (no-op if shutdown() already
                # dispatched them — _take_shutdown_callbacks() dedups).
                await ctx._run_shutdown_callbacks("session ended")
                try:
                    # Fire-and-forget in Rust (Plivo REST DELETE spawned on the
                    # endpoint's tokio runtime) — returns immediately, safe inline.
                    self._ep.hangup(session_id)
                except Exception:
                    pass
                # Cleanup Room facade. We do NOT call
                # ``_JobContextVar.reset(job_ctx_token)`` here: late
                # ``session.close`` listeners (e.g.
                # ``livekit.agents.beta.tools.end_call._on_session_close``)
                # can fire AFTER ``session.aclose()`` returns and they
                # need ``get_job_context()`` to work. The contextvar is
                # scoped to this asyncio.Task and dies cleanly when the
                # task exits, so explicit reset is unnecessary.
                room._on_session_ended()
                # No EventWaiter to close in the LiveKit-faithful model —
                # any in-flight ``capture_frame`` / ``wait_for_playout``
                # is awaiting on its own per-call subscribed Queue. The
                # Rust ``AudioBuffer::Drop`` emits an
                # ``audio_capture_error { error: "buffer_dropped" }`` for
                # every pending async_id when the session is torn down,
                # which is dispatched through the FfiQueue and resolves
                # each awaiter with a ``RuntimeError``. Per-call
                # unsubscribe runs in the audio source's ``finally``.
                self._active_sessions.pop(session_id, None)
                self._session_ended_events.pop(session_id, None)
                self._session_contexts.pop(session_id, None)
                logger.info("Session %s ended", session_id)

        task = asyncio.create_task(_run_session())
        self._active_sessions[session_id] = task
