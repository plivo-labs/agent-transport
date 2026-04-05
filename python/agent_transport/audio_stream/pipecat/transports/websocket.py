"""Rust-backed WebSocket server transport for Plivo audio streaming.

Drop-in replacement for pipecat.transports.websocket.server.WebsocketServerTransport.
Audio pacing, codec negotiation, and Plivo protocol handling are done in Rust.

Usage:
    from agent_transport.audio_stream.pipecat.serializers.plivo import PlivoFrameSerializer
    from agent_transport.audio_stream.pipecat.transports.websocket import WebsocketServerTransport

    serializer = PlivoFrameSerializer(auth_id="...", auth_token="...")
    server = WebsocketServerTransport(serializer=serializer)

    @server.setup()
    def prewarm():
        return {"vad": SileroVADAnalyzer()}

    @server.handler()
    async def run_bot(transport, userdata):
        vad = userdata["vad"]
        ...

    server.run()
"""

import asyncio
import inspect
import platform
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, Optional

from loguru import logger

from agent_transport import AudioStreamEndpoint

try:
    from pipecat.transports.base_transport import TransportParams
except ImportError:
    TransportParams = None

from ..audio_stream_transport import AudioStreamTransport
from ..serializers.plivo import PlivoFrameSerializer

try:
    import prometheus_client
    HAS_PROMETHEUS = True
except ImportError:
    HAS_PROMETHEUS = False

try:
    from aiohttp import web
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False


# ─── Prometheus metrics ──────────────────────────────────────────────────────

if HAS_PROMETHEUS:
    STREAM_SESSIONS_TOTAL = prometheus_client.Counter(
        "pipecat_audio_stream_sessions_total", "Total audio stream sessions",
        ["nodename"],
    )
    STREAM_SESSION_DURATION = prometheus_client.Histogram(
        "pipecat_audio_stream_session_duration_seconds", "Session duration",
        buckets=[1, 5, 10, 30, 60, 120, 300, 600],
    )
    RUNNING_SESSIONS_GAUGE = prometheus_client.Gauge(
        "pipecat_audio_stream_running_sessions", "Active sessions",
    )
    CPU_LOAD_GAUGE = prometheus_client.Gauge(
        "pipecat_audio_stream_cpu_load", "CPU load percent",
    )


def _session_to_dict(session) -> Dict[str, Any]:
    """Convert a PyO3 CallSession object to a plain dict for transport metadata."""
    return {
        "session_id": session.session_id,
        "call_uuid": getattr(session, "call_uuid", None) or "",
        "remote_uri": getattr(session, "remote_uri", ""),
        "local_uri": getattr(session, "local_uri", ""),
        "direction": getattr(session, "direction", ""),
        "extra_headers": getattr(session, "extra_headers", {}),
    }


@dataclass
class WebsocketServerParams:
    """Parameters for WebsocketServerTransport.

    Matches pipecat.transports.websocket.server.WebsocketServerParams structure.
    """
    serializer: Optional[PlivoFrameSerializer] = None
    transport_params: Optional["TransportParams"] = None


class WebsocketServerTransport:
    """Rust-backed WebSocket server transport for Plivo audio streaming.

    Matches pipecat.transports.websocket.server.WebsocketServerTransport interface.
    Wraps AudioStreamEndpoint (Rust) for WebSocket handling, codec negotiation,
    and checkpoint-based audio pacing. Manages session lifecycle and creates
    per-session AudioStreamTransport instances.

    Uses a single event dispatcher loop (matching LiveKit AgentServer pattern)
    to avoid event-stealing race conditions between server and per-session loops.
    """

    def __init__(
        self,
        *,
        serializer: Optional[PlivoFrameSerializer] = None,
        params: Optional[WebsocketServerParams] = None,
        transport_params: Optional["TransportParams"] = None,
        http_host: str = "0.0.0.0",
        http_port: Optional[int] = None,
    ) -> None:
        s = serializer or (params.serializer if params else None) or PlivoFrameSerializer()
        self._listen_addr = s.listen_addr
        self._plivo_auth_id = s.auth_id
        self._plivo_auth_token = s.auth_token
        self._sample_rate = s.sample_rate
        self._transport_params = transport_params or (params.transport_params if params else None)
        self._http_host = http_host
        self._http_port = http_port
        self._handler_fnc: Optional[Callable[..., Coroutine]] = None
        self._setup_fnc: Optional[Callable] = None
        self._userdata: Dict[str, Any] = {}
        self._ep: Optional[AudioStreamEndpoint] = None
        self._active_sessions: dict[str, asyncio.Task] = {}
        self._session_start_times: dict[str, float] = {}
        # Per-session event queues — server dispatches events to the right session
        self._session_event_queues: dict[str, asyncio.Queue] = {}

    @property
    def endpoint(self) -> Optional[AudioStreamEndpoint]:
        """The underlying Rust AudioStreamEndpoint, or None if not started."""
        return self._ep

    @property
    def userdata(self) -> Dict[str, Any]:
        """Shared resources from @setup(). Available in handler via userdata arg."""
        return self._userdata

    def setup(self) -> Callable:
        """Decorator to register a one-time setup function.

        Runs once before accepting sessions. Return a dict of shared resources
        (VAD models, turn detectors, etc.) — passed to every handler call.
        Avoids reloading heavy models per call::

            @server.setup()
            def prewarm():
                return {"vad": SileroVADAnalyzer()}
        """
        def decorator(fn: Callable) -> Callable:
            self._setup_fnc = fn
            return fn
        return decorator

    def handler(self) -> Callable:
        """Decorator to register the bot handler.

        Handler receives transport and shared userdata from @setup()::

            @server.handler()
            async def run_bot(transport, userdata):
                vad = userdata["vad"]
                pipeline = Pipeline([transport.input(), ...])
                await PipelineRunner().run(PipelineTask(pipeline))
        """
        def decorator(fn: Callable[..., Coroutine]) -> Callable:
            self._handler_fnc = fn
            return fn
        return decorator

    def run(self) -> None:
        """Start the server. Blocks until interrupted."""
        asyncio.run(self._run())

    async def run_async(self) -> None:
        """Start the server (async version)."""
        await self._run()

    async def _run(self) -> None:
        if self._handler_fnc is None:
            raise RuntimeError(
                "No handler registered. Use @server.handler() to define one."
            )

        # Run setup once
        if self._setup_fnc is not None:
            result = self._setup_fnc()
            if isinstance(result, dict):
                self._userdata = result
            logger.info("Setup complete: %s", list(self._userdata.keys()) or "(no userdata)")

        self._ep = AudioStreamEndpoint(
            listen_addr=self._listen_addr,
            plivo_auth_id=self._plivo_auth_id,
            plivo_auth_token=self._plivo_auth_token,
            input_sample_rate=self._sample_rate,
            output_sample_rate=self._sample_rate,
        )
        logger.info("WebSocket server listening on %s", self._listen_addr)

        # Start HTTP server if aiohttp available and port configured
        http_task = None
        if HAS_AIOHTTP and self._http_port:
            http_task = asyncio.create_task(self._run_http_server())

        try:
            await self._event_loop()
        except asyncio.CancelledError:
            pass
        except KeyboardInterrupt:
            pass
        finally:
            if self._active_sessions:
                logger.info("Draining %d active session(s)...", len(self._active_sessions))
                for task in self._active_sessions.values():
                    task.cancel()
                await asyncio.gather(*self._active_sessions.values(), return_exceptions=True)
            if http_task:
                http_task.cancel()
                try:
                    await http_task
                except (asyncio.CancelledError, Exception):
                    pass
            if self._ep:
                await asyncio.get_running_loop().run_in_executor(None, self._ep.shutdown)
            logger.info("Server shut down")

    async def _event_loop(self) -> None:
        """Single event dispatcher — reads ALL events, routes to correct session.

        Avoids event-stealing race between server loop and per-session loops.
        Matches LiveKit AgentServer._sip_event_loop pattern.
        """
        loop = asyncio.get_running_loop()
        # Sessions waiting for call_media_active after incoming_call
        pending_sessions: dict[str, dict] = {}  # session_id → session_data

        while True:
            event = await loop.run_in_executor(
                None, lambda: self._ep.wait_for_event(timeout_ms=1000)
            )
            if not event:
                continue

            ev_type = event["type"]

            if ev_type == "incoming_call":
                session = event["session"]
                session_id = session.session_id
                session_data = _session_to_dict(session)
                logger.info("Session %s connected (call_uuid=%s)",
                            session_id, session_data.get("call_uuid", ""))
                pending_sessions[session_id] = session_data

            elif ev_type == "call_media_active":
                session_id = event.get("session_id", "")
                if session_id in pending_sessions:
                    session_data = pending_sessions.pop(session_id)
                    self._start_session(session_id, session_data)

            elif ev_type == "call_terminated":
                session = event["session"]
                session_id = session.session_id
                pending_sessions.pop(session_id, None)
                # Route to per-session queue
                q = self._session_event_queues.get(session_id)
                if q:
                    await q.put(event)

            elif ev_type == "dtmf_received":
                session_id = event.get("session_id", "")
                q = self._session_event_queues.get(session_id)
                if q:
                    await q.put(event)

            elif ev_type in ("beep_detected", "beep_timeout"):
                session_id = event.get("session_id", "")
                q = self._session_event_queues.get(session_id)
                if q:
                    await q.put(event)
                else:
                    logger.warning("No session queue for %s event on session %s (session not yet started?)", ev_type, session_id)

    def _start_session(self, session_id: str, session_data: dict) -> None:
        """Create transport and spawn session handler task."""
        # Create per-session event queue
        event_queue: asyncio.Queue = asyncio.Queue()
        self._session_event_queues[session_id] = event_queue

        transport = AudioStreamTransport(
            self._ep, session_id,
            session_data=session_data,
            params=self._transport_params or TransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
            ),
            _event_queue=event_queue,
        )

        task = asyncio.create_task(self._run_session(session_id, transport))
        self._active_sessions[session_id] = task
        self._session_start_times[session_id] = time.monotonic()

    async def _run_session(self, session_id: str, transport: AudioStreamTransport) -> None:
        if HAS_PROMETHEUS:
            STREAM_SESSIONS_TOTAL.labels(nodename=platform.node()).inc()
            RUNNING_SESSIONS_GAUGE.inc()

        try:
            sig = inspect.signature(self._handler_fnc)
            if len(sig.parameters) >= 2:
                await self._handler_fnc(transport, self._userdata)
            else:
                await self._handler_fnc(transport)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Session %s handler failed", session_id)
        finally:
            duration = time.monotonic() - self._session_start_times.pop(session_id, time.monotonic())
            self._active_sessions.pop(session_id, None)
            self._session_event_queues.pop(session_id, None)
            if HAS_PROMETHEUS:
                RUNNING_SESSIONS_GAUGE.dec()
                STREAM_SESSION_DURATION.observe(duration)
            logger.info("Session %s ended (%.1fs)", session_id, duration)

    # ── HTTP server ──────────────────────────────────────────────────────

    async def _run_http_server(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._health_handler)
        app.router.add_get("/metrics", self._metrics_handler)
        app.router.add_get("/worker", self._worker_handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._http_host, self._http_port)
        logger.info("HTTP server on http://%s:%d (health, metrics, worker)",
                     self._http_host, self._http_port)
        await site.start()

        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await runner.cleanup()

    async def _health_handler(self, request: "web.Request") -> "web.Response":
        if self._ep is None:
            return web.Response(status=503, text="not ready")
        return web.Response(text="ok")

    async def _metrics_handler(self, request: "web.Request") -> "web.Response":
        if HAS_PROMETHEUS:
            RUNNING_SESSIONS_GAUGE.set(len(self._active_sessions))
            try:
                import psutil
                CPU_LOAD_GAUGE.set(psutil.cpu_percent())
            except ImportError:
                pass
            return web.Response(
                text=prometheus_client.generate_latest().decode(),
                content_type="text/plain",
            )
        return web.Response(text="prometheus_client not installed", status=501)

    async def _worker_handler(self, request: "web.Request") -> "web.Response":
        import json
        return web.Response(
            text=json.dumps({
                "worker_type": "JT_AUDIO_STREAM",
                "active_sessions": len(self._active_sessions),
                "listen_addr": self._listen_addr,
                "sample_rate": self._sample_rate,
            }),
            content_type="application/json",
        )
