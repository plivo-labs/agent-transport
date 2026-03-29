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
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, Optional

from agent_transport import AudioStreamEndpoint

logger = logging.getLogger("agent_transport.websocket_server")

try:
    from pipecat.transports.base_transport import TransportParams
except ImportError:
    TransportParams = None

from ..audio_stream_transport import AudioStreamTransport
from ..serializers.plivo import PlivoFrameSerializer


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
    and 20ms audio pacing. Manages session lifecycle and creates per-session
    AudioStreamTransport instances.

    Supports @setup() for one-time resource loading (VAD models, turn detectors)
    that are shared across all sessions — avoids reloading per call.
    """

    def __init__(
        self,
        *,
        serializer: Optional[PlivoFrameSerializer] = None,
        params: Optional[WebsocketServerParams] = None,
        transport_params: Optional["TransportParams"] = None,
    ) -> None:
        s = serializer or (params.serializer if params else None) or PlivoFrameSerializer()
        self._listen_addr = s.listen_addr
        self._plivo_auth_id = s.auth_id
        self._plivo_auth_token = s.auth_token
        self._sample_rate = s.sample_rate
        self._transport_params = transport_params or (params.transport_params if params else None)
        self._handler_fnc: Optional[Callable[..., Coroutine]] = None
        self._setup_fnc: Optional[Callable] = None
        self._userdata: Dict[str, Any] = {}
        self._ep: Optional[AudioStreamEndpoint] = None
        self._active_sessions: dict[str, asyncio.Task] = {}

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
            sample_rate=self._sample_rate,
        )
        logger.info("WebSocket server listening on %s", self._listen_addr)

        try:
            await self._session_loop()
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
            if self._ep:
                self._ep.shutdown()
            logger.info("Server shut down")

    async def _session_loop(self) -> None:
        loop = asyncio.get_running_loop()

        while True:
            event = await loop.run_in_executor(
                None, lambda: self._ep.wait_for_event(timeout_ms=0)
            )
            if not event or event["type"] != "incoming_call":
                continue

            session = event.get("session", {})
            session_id = session.get("call_id", "")
            logger.info("Session %s connected (call_uuid=%s)",
                        session_id, session.get("call_uuid", ""))

            # Wait for media active
            await loop.run_in_executor(
                None, lambda: self._ep.wait_for_event(timeout_ms=5000)
            )

            transport = AudioStreamTransport(
                self._ep, session_id,
                session_data=session,
                params=self._transport_params or TransportParams(
                    audio_in_enabled=True,
                    audio_out_enabled=True,
                ),
            )

            task = asyncio.create_task(self._run_session(session_id, transport))
            self._active_sessions[session_id] = task

    async def _run_session(self, session_id: str, transport: AudioStreamTransport) -> None:
        try:
            import inspect
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
            self._active_sessions.pop(session_id, None)
            logger.info("Session %s ended", session_id)
