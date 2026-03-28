"""AudioStreamServer — drop-in equivalent of AgentServer for Plivo audio streaming.

Same pattern as AgentServer but over WebSocket instead of SIP:
    server = AudioStreamServer(listen_addr="0.0.0.0:8765")

    @server.audio_stream_session()
    async def entrypoint(ctx: AudioStreamCallContext):
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
from livekit.agents.inference_runner import _InferenceRunner
from livekit.agents.utils.hw import get_cpu_monitor
from livekit.agents.utils import MovingAverage
from .audio_stream_io import AudioStreamInput, AudioStreamOutput
from ._room_facade import TransportRoom, create_transport_context
from livekit.rtc.room import SipDTMF

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
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()

    def _sample_loop(self) -> None:
        while True:
            cpu = self._cpu_monitor.cpu_percent(interval=0.5)
            with self._lock:
                self._avg.add_sample(cpu)

    def get_load(self) -> float:
        with self._lock:
            return self._avg.get_avg()


# ─── AudioStreamCallContext ───────────────────────────────────────────────────

@dataclass
class AudioStreamCallContext:
    """Context passed to the @audio_stream_session handler.

    Matches LiveKit's standard pattern exactly:
        @server.audio_stream_session()
        async def entrypoint(ctx: AudioStreamCallContext):
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
    call_id: str              # Plivo Call UUID
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

        # Wire audio stream I/O before session.start() is called
        session.input.audio = AudioStreamInput(self.endpoint, self.session_id)
        session.output.audio = AudioStreamOutput(self.endpoint, self.session_id)

        # Listen to session close event — handles agent-initiated shutdown
        @session.on("close")
        def _on_session_close(ev):
            logger.info("Session %s closed (reason=%s)", self.session_id, getattr(ev, 'reason', 'unknown'))
            if self._call_ended is not None and not self._call_ended.is_set():
                self._call_ended.set()
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
        sample_rate: int = 16000,
        host: str = "0.0.0.0",
        port: int | None = None,
        agent_name: str = "audio-stream-agent",
        auth: Callable[..., bool | Coroutine] | None = None,
    ) -> None:
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
        self._userdata: dict[str, Any] = {}
        self._ep: AudioStreamEndpoint | None = None
        self._active_sessions: dict[int, asyncio.Task] = {}
        self._session_ended_events: dict[int, asyncio.Event] = {}
        self._session_contexts: dict[int, AudioStreamCallContext] = {}
        self._load_monitor = _LoadMonitor()

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

        app()

    async def _run(self, *, log_mode: str = "start") -> None:
        self._configure_logging(log_mode)

        if self._entrypoint_fnc is None:
            logger.error(
                "No audio stream session entrypoint registered.\n"
                "Define one using the @server.audio_stream_session() decorator, for example:\n"
                '    @server.audio_stream_session()\n'
                "    async def entrypoint(ctx: AudioStreamCallContext):\n"
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

        # Run user's setup function
        if self._setup_fnc:
            if self._inference_executor:
                _set_inference_context(self._inference_executor)
            result = self._setup_fnc()
            if self._inference_executor:
                _clear_inference_context()
            if isinstance(result, dict):
                self._userdata = result
            logger.info("Setup complete: %s", list(self._userdata.keys()))

        # Create AudioStreamEndpoint (starts WS server immediately)
        self._ep = AudioStreamEndpoint(
            listen_addr=self._listen_addr,
            plivo_auth_id=self._plivo_auth_id,
            plivo_auth_token=self._plivo_auth_token,
            sample_rate=self._sample_rate,
        )
        logger.info("Audio stream WebSocket server on ws://%s", self._listen_addr)

        # Start HTTP server
        http_app = self._build_http_app()
        runner = web.AppRunner(http_app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port, reuse_address=True)
        await site.start()
        logger.info("HTTP server on http://%s:%d", self._host, self._port)

        # Start event loop
        event_task = asyncio.create_task(self._event_loop())

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
        self._ep.shutdown()

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

    async def _event_loop(self) -> None:
        """Event dispatcher — reads audio stream events and routes them.

        Audio streaming fires incoming_call + call_media_active together
        on the Plivo 'start' event. We dispatch on incoming_call and
        consume call_media_active to prevent queue buildup.
        """
        loop = asyncio.get_running_loop()

        while True:
            try:
                ev = await loop.run_in_executor(None, self._ep.wait_for_event, 1000)
            except Exception:
                break

            if not ev:
                continue

            ev_type = ev["type"]

            if ev_type == "incoming_call":
                session = ev["session"]
                session_id = session.call_id  # internal session ID
                call_id = session.remote_uri  # Plivo Call UUID
                stream_id = session.local_uri if hasattr(session, "local_uri") else ""
                extra_headers = session.extra_headers if hasattr(session, "extra_headers") else {}
                logger.info("Audio stream session %s started (call_id=%s, stream_id=%s)", session_id, call_id, stream_id)
                asyncio.create_task(
                    self._start_session(session_id, call_id, stream_id, extra_headers)
                )

            elif ev_type == "call_media_active":
                # Consumed — audio stream fires this together with incoming_call
                pass

            elif ev_type == "call_terminated":
                session_id = ev["session"].call_id
                reason = ev.get("reason", "unknown")
                logger.info("Session %s terminated (reason=%s)", session_id, reason)

                # Emit participant_disconnected on Room facade (matches LiveKit WebRTC)
                # RoomIO._on_participant_disconnected will call _close_soon() → session closes
                ctx = self._session_contexts.get(session_id)
                if ctx and ctx._room:
                    remote = ctx._room._remote
                    remote.disconnect_reason = 1  # CLIENT_INITIATED
                    ctx._room.emit("participant_disconnected", remote)

                if session_id in self._session_ended_events:
                    self._session_ended_events[session_id].set()

            elif ev_type == "dtmf_received":
                # Route DTMF to both ctx listeners AND Room facade
                session_id = ev.get("call_id", -1)
                digit = ev.get("digit", "")
                logger.debug("DTMF '%s' on session %s", digit, session_id)
                ctx = self._session_contexts.get(session_id)
                if ctx:
                    # Emit on ctx for simple ctx.on("dtmf_received") pattern
                    ctx._emit("dtmf_received", digit)
                    # Emit on Room facade for LiveKit GetDtmfTask compatibility
                    # room.on("sip_dtmf_received", handler) receives SipDTMF(code, digit, participant)
                    if ctx._room:
                        dtmf_ev = SipDTMF(code=ord(digit) if digit else 0, digit=digit,
                                          participant=ctx._room._remote)
                        ctx._room.emit("sip_dtmf_received", dtmf_ev)

    async def _start_session(self, session_id: str, call_id: str, stream_id: str, extra_headers: dict) -> None:
        session_ended = asyncio.Event()
        self._session_ended_events[session_id] = session_ended

        # Create Room facade BEFORE handler runs — ctx.room is available immediately
        room = TransportRoom(
            self._ep, session_id,
            agent_name=self._agent_name,
            caller_identity=call_id,
        )
        # Set on JobContext so get_job_context().room works inside handler
        job_stub, job_ctx_token = create_transport_context(
            room, agent_name=self._agent_name)

        ctx = AudioStreamCallContext(
            session_id=session_id,
            call_id=call_id,
            stream_id=stream_id,
            direction="inbound",
            extra_headers=extra_headers,
            endpoint=self._ep,
            userdata=self._userdata,
            _agent_name=self._agent_name,
            _call_ended=session_ended,
            _room=room,
            _job_stub=job_stub,
            _job_ctx_token=job_ctx_token,
        )
        self._session_contexts[session_id] = ctx

        async def _run_session():
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
                try:
                    self._ep.hangup(session_id)
                except Exception:
                    pass
                # Cleanup Room facade and JobContext
                room._on_session_ended()
                from livekit.agents.job import _JobContextVar
                try:
                    _JobContextVar.reset(job_ctx_token)
                except ValueError:
                    pass
                self._active_sessions.pop(session_id, None)
                self._session_ended_events.pop(session_id, None)
                self._session_contexts.pop(session_id, None)
                logger.info("Session %s ended", session_id)

        task = asyncio.create_task(_run_session())
        self._active_sessions[session_id] = task
