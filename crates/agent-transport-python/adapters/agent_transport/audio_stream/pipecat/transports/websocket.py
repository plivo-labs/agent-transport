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

Shutdown behavior (SIGINT/SIGTERM): same as the Pipecat SIP transport —
hangup active sessions, close endpoint (2s), then ``os._exit(0)``. Flush
recordings / observability per-session, not at server shutdown.
"""

import asyncio
import inspect
import platform
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, Optional

from loguru import logger

from agent_transport import AudioStreamEndpoint
from agent_transport._event_sink import _on_event_from_rust
from agent_transport._ffi_queue import GLOBAL_DICT

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

    The server runs one dispatcher loop that subscribes to
    ``GLOBAL_DICT`` and routes events to per-session asyncio queues
    consumed by each ``AudioStreamInputTransport``. Audio backpressure
    completion events are routed too, so OutputTransport's
    ``write_audio_frame`` ``wait_for`` resolves.
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

        # Run setup once — support both sync and async setup functions.
        if self._setup_fnc is not None:
            try:
                if inspect.iscoroutinefunction(self._setup_fnc):
                    result = await self._setup_fnc()
                else:
                    result = self._setup_fnc()
                    # Tolerate a sync function that returns an awaitable.
                    if inspect.isawaitable(result):
                        result = await result
                if isinstance(result, dict):
                    self._userdata = result
            except Exception:
                logger.exception("Setup function failed")
                raise
            logger.info("Setup complete: {}", list(self._userdata.keys()) or "(no userdata)")

        self._ep = AudioStreamEndpoint(
            listen_addr=self._listen_addr,
            plivo_auth_id=self._plivo_auth_id,
            plivo_auth_token=self._plivo_auth_token,
            input_sample_rate=self._sample_rate,
            output_sample_rate=self._sample_rate,
        )
        # Install the shared event sink so events flow into GLOBAL_DICT
        # (and the LiveKit-shape GLOBAL alongside it). Required so a
        # process hosting both pipecat + a LiveKit adapter doesn't see
        # one of them silently lose events.
        self._ep.set_event_sink(_on_event_from_rust)
        logger.info("WebSocket server listening on ws://{}", self._listen_addr)

        # Start HTTP server if aiohttp available and port configured
        http_task = None
        if HAS_AIOHTTP and self._http_port:
            http_task = asyncio.create_task(self._run_http_server())

        # Install SIGTERM handler so container stops hit the finally block.
        # SIGINT is already turned into CancelledError by asyncio's default
        # handler; SIGTERM without an explicit handler would kill abruptly.
        import signal as _signal
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, ValueError):
                pass
        event_loop_task = asyncio.create_task(self._event_loop())
        stop_task = asyncio.create_task(stop.wait())

        try:
            done, _pending = await asyncio.wait(
                {event_loop_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Surface any crash from the event loop task before we tear
            # everything down — otherwise Python GC logs
            # "Task exception was never retrieved" and we lose the signal.
            if event_loop_task in done and not event_loop_task.cancelled():
                exc = event_loop_task.exception()
                if exc is not None:
                    logger.error("Audio stream event loop crashed: {}", exc)
        except asyncio.CancelledError:
            pass
        except KeyboardInterrupt:
            pass
        finally:
            # Hang up active sessions first, then close the endpoint (which
            # also cascade-hangs-up anything left), then force-exit. Rust
            # owns background threads that pin the process — os._exit is the
            # only reliable way out.
            import os as _os
            import sys as _sys
            try:
                if self._ep is not None:
                    for session_id in list(self._active_sessions.keys()):
                        try:
                            self._ep.hangup(session_id)
                        except Exception:
                            pass
                for task in self._active_sessions.values():
                    task.cancel()
                event_loop_task.cancel()
                stop_task.cancel()
                if http_task:
                    http_task.cancel()
                    try:
                        await asyncio.wait_for(http_task, timeout=1.0)
                    except Exception:
                        pass
                if self._ep is not None:
                    try:
                        await asyncio.wait_for(
                            loop.run_in_executor(None, self._ep.shutdown),
                            timeout=2.0,
                        )
                    except Exception:
                        pass
            finally:
                logger.info("Server shut down")
                # Flush stdio so the last log lines aren't lost —
                # os._exit skips normal Python finalization.
                try:
                    _sys.stdout.flush()
                    _sys.stderr.flush()
                except Exception:
                    pass
                _os._exit(0)

    async def _event_loop(self) -> None:
        """Single event dispatcher — reads ALL events, routes to correct session.

        Subscribes to ``GLOBAL_DICT`` (the dict-shaped FfiQueue fed by
        the shared event sink). Each event is dispatched to either a
        server-level path (``call_answered`` creates a session) or the
        matching per-session asyncio queue consumed by
        ``AudioStreamInputTransport._event_loop_from_queue``. Audio
        async-id events are routed here too so OutputTransport's
        per-frame ``wait_for`` actually completes — previously the
        server only routed lifecycle events and audio backpressure
        was silently dropped.
        """
        queue = GLOBAL_DICT.subscribe()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    logger.exception("audio_stream event loop fetch failed")
                    break

                try:
                    ev_type = event.get("type", "")

                    if ev_type == "shutdown":
                        logger.debug("pipecat audio_stream event loop received shutdown sentinel")
                        break

                    if ev_type == "call_answered":
                        session = event["session"]
                        session_id = session.session_id
                        if session_id in self._active_sessions:
                            continue
                        session_data = _session_to_dict(session)
                        logger.info("Session {} connected (call_uuid={})",
                                    session_id, session_data.get("call_uuid", ""))
                        self._start_session(session_id, session_data)

                    elif ev_type == "call_terminated":
                        session = event["session"]
                        session_id = session.session_id
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
                            logger.warning("No session queue for {} event on session {} (session not yet started?)", ev_type, session_id)

                    elif ev_type in (
                        "audio_capture_complete",
                        "audio_playout_complete",
                        "audio_buffer_drained",
                        "audio_capture_error",
                    ):
                        # Per-frame backpressure: route to the matching
                        # session queue so InputTransport's
                        # _handle_event forwards into the transport's
                        # private FfiQueue.
                        session_id = event.get("session_id", "")
                        q = self._session_event_queues.get(session_id)
                        if q:
                            await q.put(event)
                except Exception:
                    logger.exception("Error handling WS event %r", event.get("type") if isinstance(event, dict) else event)
        finally:
            GLOBAL_DICT.unsubscribe(queue)

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
            logger.exception("Session {} handler failed", session_id)
        finally:
            duration = time.monotonic() - self._session_start_times.pop(session_id, time.monotonic())
            self._active_sessions.pop(session_id, None)
            self._session_event_queues.pop(session_id, None)
            if HAS_PROMETHEUS:
                RUNNING_SESSIONS_GAUGE.dec()
                STREAM_SESSION_DURATION.observe(duration)
            logger.info("Session {} ended ({:.1f}s)", session_id, duration)

    # ── HTTP server ──────────────────────────────────────────────────────

    async def _run_http_server(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._health_handler)
        app.router.add_get("/metrics", self._metrics_handler)
        app.router.add_get("/worker", self._worker_handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._http_host, self._http_port)
        logger.info("HTTP server on http://{}:{} (health, metrics, worker)",
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
