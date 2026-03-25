"""AgentServer — drop-in equivalent of LiveKit's AgentServer for SIP transport.

Matches LiveKit's pattern:
    server = AgentServer()

    @server.sip_session()
    async def entrypoint(ctx: CallContext):
        session = AgentSession(vad=..., stt=..., llm=..., tts=...)
        await ctx.start(session, agent=Assistant())

    if __name__ == "__main__":
        run_app(server)

CLI commands (matching LiveKit):
    python agent.py start   — production mode (INFO logging)
    python agent.py dev     — development mode (DEBUG for adapters/pipeline)
    python agent.py debug   — full debug (including Rust SIP/RTP)
"""

import asyncio
import json
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

from agent_transport import SipEndpoint, init_logging
from livekit.agents.utils.hw import get_cpu_monitor
from livekit.agents.utils import MovingAverage
from .sip_io import SipAudioInput, SipAudioOutput

logger = logging.getLogger("agent_transport.server")

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


@dataclass
class CallContext:
    """Context passed to the @sip_session handler — equivalent of LiveKit's JobContext.

    Use ctx.start(session, agent=) to wire SIP audio and start the agent.
    """

    call_id: int
    remote_uri: str
    direction: str  # "inbound" or "outbound"
    endpoint: SipEndpoint
    extra_headers: dict[str, str] = field(default_factory=dict)

    _session: Any = field(default=None, repr=False)
    _call_ended: asyncio.Event | None = field(default=None, repr=False)

    async def start(self, session: Any, *, agent: Any, **kwargs) -> None:
        """Wire SIP audio I/O, start the agent session, and wait for call to end.

        Equivalent of: await session.start(agent=, room=ctx.room)
        Blocks until the SIP call is terminated (BYE received).
        """
        session.input.audio = SipAudioInput(self.endpoint, self.call_id)
        session.output.audio = SipAudioOutput(self.endpoint, self.call_id)
        self._session = session

        # Auto-log state changes in dev/debug mode
        if logging.getLogger("agent_transport.sip").isEnabledFor(logging.DEBUG):
            @session.on("agent_state_changed")
            def _on_agent_state(ev):
                logger.info("Call %d agent: %s → %s", self.call_id, ev.old_state, ev.new_state)
            @session.on("user_state_changed")
            def _on_user_state(ev):
                logger.info("Call %d user: %s → %s", self.call_id, ev.old_state, ev.new_state)

        await session.start(agent=agent, **kwargs)

        # Wait for call end via the event dispatcher (set by AgentServer)
        if self._call_ended is not None:
            await self._call_ended.wait()


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
        agent_name: str = "sip-agent",
        auth: Callable[..., bool | Coroutine] | None = None,
        recording: bool = False,
        recording_dir: str = "recordings",
        recording_stereo: bool = True,
    ) -> None:
        self._sip_server = sip_server or os.environ.get("SIP_DOMAIN", "phone.plivo.com")
        self._sip_port = sip_port or int(os.environ.get("SIP_PORT", "5060"))
        self._sip_username = sip_username or os.environ.get("SIP_USERNAME", "")
        self._sip_password = sip_password or os.environ.get("SIP_PASSWORD", "")
        self._host = host
        self._port = port or int(os.environ.get("PORT", "8080"))
        self._agent_name = agent_name
        self._auth = auth
        self._recording = recording
        self._recording_dir = recording_dir
        self._recording_stereo = recording_stereo
        self._entrypoint_fnc: Callable[..., Coroutine] | None = None
        self._ep: SipEndpoint | None = None
        self._active_calls: dict[int, asyncio.Task] = {}
        self._call_ended_events: dict[int, asyncio.Event] = {}
        self._pending_outbound: dict[int, asyncio.Future] = {}
        self._load_monitor = _LoadMonitor()

    def sip_session(self) -> Callable:
        """Decorator to register the call handler — equivalent of @server.rtc_session()."""
        def decorator(fn: Callable[..., Coroutine]) -> Callable:
            self._entrypoint_fnc = fn
            return fn
        return decorator

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
                "    async def entrypoint(ctx: CallContext):\n"
                "        ..."
            )
            sys.exit(1)

        self._ep = SipEndpoint(sip_server=self._sip_server)
        loop = asyncio.get_running_loop()

        # Register with SIP provider
        await loop.run_in_executor(
            None, self._ep.register, self._sip_username, self._sip_password
        )
        ev = await loop.run_in_executor(None, self._ep.wait_for_event, 10000)
        if not ev or ev["type"] != "registered":
            logger.error("SIP registration failed: %s", ev)
            sys.exit(1)

        logger.info("Registered as %s@%s:%d", self._sip_username, self._sip_server, self._sip_port)

        # Start HTTP server
        http_app = self._build_http_app()
        runner = web.AppRunner(http_app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port, reuse_address=True)
        await site.start()
        logger.info("HTTP server on http://%s:%d", self._host, self._port)

        # Start SIP event loop
        event_task = asyncio.create_task(self._sip_event_loop())

        # Wait for shutdown signal
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        await stop.wait()
        logger.info("Shutting down...")
        event_task.cancel()

        if self._active_calls:
            logger.info("Draining %d active call(s)...", len(self._active_calls))
            await asyncio.gather(*self._active_calls.values(), return_exceptions=True)

        await runner.cleanup()
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

        destination = data.get("to", "")
        if not destination:
            return web.json_response({"error": "missing 'to' field"}, status=400)

        loop = asyncio.get_running_loop()
        try:
            call_id = await loop.run_in_executor(None, self._ep.make_call, destination)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

        logger.info("Outbound call %d to %s", call_id, destination)

        # Register a future that the event dispatcher will resolve on call_media_active
        media_fut = asyncio.get_running_loop().create_future()
        self._pending_outbound[call_id] = media_fut

        async def _wait_and_handle():
            try:
                await asyncio.wait_for(media_fut, timeout=30)
            except asyncio.TimeoutError:
                logger.warning("Outbound call %d to %s timed out", call_id, destination)
                self._pending_outbound.pop(call_id, None)
                return
            # Dispatcher already popped _pending_outbound
            if media_fut.result():
                await self._start_call(call_id, destination, direction="outbound")

        asyncio.create_task(_wait_and_handle())
        return web.json_response({"status": "calling", "call_id": call_id, "to": destination})

    async def _sip_event_loop(self) -> None:
        """Single event dispatcher — reads all SIP events and routes them.

        Avoids multiple consumers racing on wait_for_event.
        """
        loop = asyncio.get_running_loop()
        # Inbound calls waiting for call_media_active: {call_id: remote_uri}
        pending_inbound: dict[int, str] = {}

        while True:
            try:
                ev = await loop.run_in_executor(None, self._ep.wait_for_event, 1000)
            except Exception:
                break

            if not ev:
                continue

            ev_type = ev["type"]

            if ev_type == "incoming_call":
                call_id = ev["session"].call_id
                remote_uri = ev["session"].remote_uri
                logger.info("Incoming call %d from %s", call_id, remote_uri)
                await loop.run_in_executor(None, self._ep.answer, call_id)
                pending_inbound[call_id] = remote_uri

            elif ev_type == "call_media_active":
                call_id = ev["call_id"]

                if call_id in pending_inbound:
                    remote_uri = pending_inbound.pop(call_id)
                    asyncio.create_task(
                        self._start_call(call_id, remote_uri, direction="inbound")
                    )
                elif call_id in self._pending_outbound:
                    # Outbound call — media ready
                    fut = self._pending_outbound.pop(call_id)
                    if not fut.done():
                        fut.set_result(True)

            elif ev_type == "call_terminated":
                call_id = ev["session"].call_id

                # Clean up pending inbound if call died before media
                pending_inbound.pop(call_id, None)

                # Clean up pending outbound
                if call_id in self._pending_outbound:
                    fut = self._pending_outbound.pop(call_id)
                    if not fut.done():
                        fut.set_result(False)

                # Signal active call to end
                if call_id in self._call_ended_events:
                    self._call_ended_events[call_id].set()

    async def _start_call(self, call_id: int, remote_uri: str, direction: str) -> None:
        call_ended = asyncio.Event()
        self._call_ended_events[call_id] = call_ended

        ctx = CallContext(
            call_id=call_id,
            remote_uri=remote_uri,
            direction=direction,
            endpoint=self._ep,
            _call_ended=call_ended,
        )

        async def _run_call():
            node = _nodename()
            SIP_CALLS_TOTAL.labels(nodename=node, direction=direction).inc()
            call_start = time.monotonic()

            # Start recording if enabled
            if self._recording:
                try:
                    os.makedirs(self._recording_dir, exist_ok=True)
                    rec_path = os.path.join(self._recording_dir, f"call_{call_id}.wav")
                    self._ep.start_recording(call_id, rec_path, self._recording_stereo)
                except Exception:
                    logger.warning("Failed to start recording for call %d", call_id, exc_info=True)

            try:
                await self._entrypoint_fnc(ctx)
            except Exception:
                logger.exception("Call %d handler failed", call_id)
            finally:
                SIP_CALL_DURATION.labels(nodename=node).observe(time.monotonic() - call_start)

                # Stop recording
                if self._recording:
                    try:
                        self._ep.stop_recording(call_id)
                    except Exception:
                        pass

                if ctx._session is not None:
                    try:
                        usage = ctx._session.usage
                        if usage and usage.model_usage:
                            logger.info("Call %d usage: %s", call_id, usage)
                    except Exception:
                        pass
                    try:
                        await ctx._session.aclose()
                    except Exception:
                        pass
                try:
                    self._ep.hangup(call_id)
                except Exception:
                    pass
                self._active_calls.pop(call_id, None)
                self._call_ended_events.pop(call_id, None)
                logger.info("Call %d ended (%s)", call_id, direction)

        task = asyncio.create_task(_run_call())
        self._active_calls[call_id] = task


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

    app()
