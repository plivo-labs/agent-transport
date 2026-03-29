"""SIP server transport for Pipecat pipelines.

Manages SipEndpoint lifecycle, SIP registration, session acceptance,
and per-session SipTransport creation.

Usage:
    from agent_transport.sip.pipecat import SipServerTransport

    server = SipServerTransport(sip_username="...", sip_password="...")

    @server.setup()
    def prewarm():
        return {"vad": SileroVADAnalyzer()}

    @server.handler()
    async def run_bot(transport, userdata):
        ...

    server.run()
"""

import asyncio
import inspect
import logging
import os
import platform
import time
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Dict, List, Optional

from agent_transport import SipEndpoint

logger = logging.getLogger("agent_transport.sip_server")

try:
    from pipecat.transports.base_transport import TransportParams
except ImportError:
    TransportParams = None

from ..sip_transport import SipTransport

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
    SIP_CALLS_TOTAL = prometheus_client.Counter(
        "pipecat_sip_calls_total", "Total SIP calls",
        ["nodename", "direction"],
    )
    SIP_CALL_DURATION = prometheus_client.Histogram(
        "pipecat_sip_call_duration_seconds", "SIP call duration",
        buckets=[1, 5, 10, 30, 60, 120, 300, 600],
    )
    RUNNING_CALLS_GAUGE = prometheus_client.Gauge(
        "pipecat_sip_running_calls", "Active SIP calls",
    )
    CPU_LOAD_GAUGE = prometheus_client.Gauge(
        "pipecat_sip_cpu_load", "CPU load percent",
    )


@dataclass
class SipServerParams:
    """Parameters for SipServerTransport."""
    sip_server: str = "phone.plivo.com"
    sip_username: str = ""
    sip_password: str = ""
    stun_server: str = "stun-fb.plivo.com:3478"
    codecs: Optional[List[str]] = None
    log_level: int = 3
    jitter_buffer: bool = False
    plc: bool = False
    comfort_noise: bool = False
    transport_params: Optional["TransportParams"] = None


class SipServerTransport:
    """SIP server transport for Pipecat pipelines.

    Wraps SipEndpoint (Rust) and handles:
    - SIP registration lifecycle
    - Incoming call acceptance + session management
    - Outbound calls via call() method
    - Per-session SipTransport creation
    - @setup() for one-time model loading (VAD, turn detector)
    - @handler() for per-session bot logic
    - HTTP endpoints: /health, /metrics, /call
    - Prometheus metrics: call count, duration, CPU
    """

    def __init__(
        self,
        *,
        sip_server: Optional[str] = None,
        sip_username: Optional[str] = None,
        sip_password: Optional[str] = None,
        stun_server: Optional[str] = None,
        codecs: Optional[List[str]] = None,
        log_level: int = 3,
        jitter_buffer: bool = False,
        plc: bool = False,
        comfort_noise: bool = False,
        http_host: str = "0.0.0.0",
        http_port: Optional[int] = None,
        params: Optional[SipServerParams] = None,
        transport_params: Optional["TransportParams"] = None,
    ) -> None:
        p = params or SipServerParams()
        self._sip_server = sip_server or os.environ.get("SIP_DOMAIN", p.sip_server)
        self._sip_username = sip_username or os.environ.get("SIP_USERNAME", p.sip_username)
        self._sip_password = sip_password or os.environ.get("SIP_PASSWORD", p.sip_password)
        self._stun_server = stun_server or p.stun_server
        self._codecs = codecs or p.codecs
        self._log_level = log_level or p.log_level
        self._jitter_buffer = jitter_buffer or p.jitter_buffer
        self._plc = plc or p.plc
        self._comfort_noise = comfort_noise or p.comfort_noise
        self._http_host = http_host
        self._http_port = http_port or int(os.environ.get("PORT", "8080"))
        self._transport_params = transport_params or (p.transport_params if params else None)

        self._handler_fnc: Optional[Callable[..., Coroutine]] = None
        self._setup_fnc: Optional[Callable] = None
        self._userdata: Dict[str, Any] = {}
        self._ep: Optional[SipEndpoint] = None
        self._active_sessions: Dict[str, asyncio.Task] = {}
        self._session_start_times: Dict[str, float] = {}

    @property
    def endpoint(self) -> Optional[SipEndpoint]:
        """The underlying Rust SipEndpoint, or None if not started."""
        return self._ep

    @property
    def userdata(self) -> Dict[str, Any]:
        """Shared resources from @setup()."""
        return self._userdata

    def setup(self) -> Callable:
        """Decorator to register a one-time setup function.

        Runs once before accepting calls. Return a dict of shared resources
        (VAD models, turn detectors, etc.) — passed to every handler call::

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
                pipeline = Pipeline([transport.input(), ...])
                await PipelineRunner().run(PipelineTask(pipeline))
        """
        def decorator(fn: Callable[..., Coroutine]) -> Callable:
            self._handler_fnc = fn
            return fn
        return decorator

    async def call(self, dest_uri: str, headers: Optional[Dict[str, str]] = None) -> Optional[str]:
        """Make an outbound SIP call.

        Args:
            dest_uri: SIP URI to call (e.g., sip:+1234567890@provider.com)
            headers: Optional custom SIP headers

        Returns:
            call_id if successful, None otherwise.
        """
        if not self._ep:
            raise RuntimeError("Server not started")
        loop = asyncio.get_running_loop()
        try:
            call_id = await loop.run_in_executor(
                None, lambda: self._ep.call(dest_uri, headers)
            )
            return call_id
        except Exception as e:
            logger.error("Outbound call failed: %s", e)
            return None

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

        # Create SIP endpoint
        self._ep = SipEndpoint(
            sip_server=self._sip_server,
            stun_server=self._stun_server,
            codecs=self._codecs,
            log_level=self._log_level,
            jitter_buffer=self._jitter_buffer,
            plc=self._plc,
            comfort_noise=self._comfort_noise,
        )

        # Register with SIP server
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, lambda: self._ep.register(self._sip_username, self._sip_password)
        )

        event = await loop.run_in_executor(
            None, lambda: self._ep.wait_for_event(timeout_ms=10000)
        )
        if not event or event["type"] != "registered":
            logger.error("SIP registration failed: %s", event)
            return

        logger.info("Registered as %s@%s", self._sip_username, self._sip_server)

        # Start HTTP server if aiohttp available
        http_task = None
        if HAS_AIOHTTP:
            http_task = asyncio.create_task(self._run_http_server())

        try:
            await self._session_loop()
        except asyncio.CancelledError:
            pass
        except KeyboardInterrupt:
            pass
        finally:
            # Drain active sessions
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
                try:
                    await loop.run_in_executor(None, self._ep.shutdown)
                except Exception:
                    pass
            logger.info("Server shut down")

    async def _session_loop(self) -> None:
        loop = asyncio.get_running_loop()

        while True:
            event = await loop.run_in_executor(
                None, lambda: self._ep.wait_for_event(timeout_ms=1000)
            )
            if not event:
                continue

            if event["type"] == "incoming_call":
                session = event.get("session", {})
                call_id = session.get("call_id", "")
                remote_uri = session.get("remote_uri", "")
                logger.info("Incoming call from %s (call_id=%s)", remote_uri, call_id)

                # Answer the call
                await loop.run_in_executor(None, lambda: self._ep.answer(call_id))

                # Wait for media active
                while True:
                    ev = await loop.run_in_executor(
                        None, lambda: self._ep.wait_for_event(timeout_ms=500)
                    )
                    if ev and ev["type"] == "call_media_active":
                        break

                transport = SipTransport(
                    self._ep, call_id,
                    session_data=session,
                    params=self._transport_params or TransportParams(
                        audio_in_enabled=True,
                        audio_out_enabled=True,
                    ),
                )

                task = asyncio.create_task(self._run_session(call_id, transport))
                self._active_sessions[call_id] = task
                self._session_start_times[call_id] = time.monotonic()

    async def _run_session(self, call_id: str, transport: SipTransport) -> None:
        if HAS_PROMETHEUS:
            SIP_CALLS_TOTAL.labels(
                nodename=platform.node(), direction=transport.direction or "inbound",
            ).inc()
            RUNNING_CALLS_GAUGE.inc()

        try:
            sig = inspect.signature(self._handler_fnc)
            if len(sig.parameters) >= 2:
                await self._handler_fnc(transport, self._userdata)
            else:
                await self._handler_fnc(transport)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Session %s handler failed", call_id)
        finally:
            duration = time.monotonic() - self._session_start_times.pop(call_id, time.monotonic())
            self._active_sessions.pop(call_id, None)
            if HAS_PROMETHEUS:
                RUNNING_CALLS_GAUGE.dec()
                SIP_CALL_DURATION.observe(duration)
            logger.info("Session %s ended (%.1fs)", call_id, duration)

    # ── HTTP server ──────────────────────────────────────────────────────

    async def _run_http_server(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._health_handler)
        app.router.add_get("/metrics", self._metrics_handler)
        app.router.add_post("/call", self._call_handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._http_host, self._http_port)
        logger.info("HTTP server on http://%s:%d (health, metrics, call)",
                     self._http_host, self._http_port)
        await site.start()

        # Keep running until cancelled
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await runner.cleanup()

    async def _health_handler(self, request: "web.Request") -> "web.Response":
        if self._ep is None:
            return web.Response(status=503, text="not ready")
        registered = self._ep.is_registered()
        if not registered:
            return web.Response(status=503, text="not registered")
        return web.Response(text="ok")

    async def _metrics_handler(self, request: "web.Request") -> "web.Response":
        if HAS_PROMETHEUS:
            RUNNING_CALLS_GAUGE.set(len(self._active_sessions))
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

    async def _call_handler(self, request: "web.Request") -> "web.Response":
        """POST /call — make outbound call. Body: {"to": "sip:...", "headers": {...}}"""
        try:
            body = await request.json()
            dest = body.get("to", "")
            headers = body.get("headers")
            if not dest:
                return web.json_response({"error": "missing 'to' field"}, status=400)
            call_id = await self.call(dest, headers)
            if call_id:
                return web.json_response({"call_id": call_id})
            return web.json_response({"error": "call failed"}, status=500)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
