"""AudioStreamServer — server wrapper for Pipecat audio streaming.

Same decorator pattern as the LiveKit AudioStreamServer but for Pipecat pipelines:

    from agent_transport.audio_stream.pipecat import AudioStreamServer, AudioStreamTransport

    server = AudioStreamServer()

    @server.handler()
    async def run_bot(transport: AudioStreamTransport):
        pipeline = Pipeline([transport.input(), stt, llm, tts, transport.output()])
        task = PipelineTask(pipeline, params=PipelineParams(...))

        @transport.event_handler("on_client_connected")
        async def on_connected(transport):
            await task.queue_frames([LLMRunFrame()])

        @transport.event_handler("on_client_disconnected")
        async def on_disconnected(transport):
            await task.cancel()

        await PipelineRunner().run(task)

    if __name__ == "__main__":
        server.run()

Manages AudioStreamEndpoint lifecycle, session acceptance, transport creation,
and concurrent session handling. Keeps bot code minimal and transport-agnostic.
"""

import asyncio
import logging
import os
from typing import Callable, Coroutine, Optional

from agent_transport import AudioStreamEndpoint

logger = logging.getLogger("agent_transport.audio_stream_server")

try:
    from pipecat.transports.base_transport import TransportParams
except ImportError:
    TransportParams = None

from .audio_stream_transport import AudioStreamTransport


class AudioStreamServer:
    """Plivo audio streaming server for Pipecat pipelines.

    Wraps AudioStreamEndpoint (Rust) and handles:
    - Endpoint lifecycle (create, shutdown)
    - Session acceptance loop
    - AudioStreamTransport creation per session
    - Concurrent session management
    """

    def __init__(
        self,
        *,
        listen_addr: Optional[str] = None,
        plivo_auth_id: Optional[str] = None,
        plivo_auth_token: Optional[str] = None,
        sample_rate: int = 16000,
        params: Optional["TransportParams"] = None,
    ) -> None:
        self._listen_addr = listen_addr or os.environ.get("AUDIO_STREAM_ADDR", "0.0.0.0:8080")
        self._plivo_auth_id = plivo_auth_id or os.environ.get("PLIVO_AUTH_ID", "")
        self._plivo_auth_token = plivo_auth_token or os.environ.get("PLIVO_AUTH_TOKEN", "")
        self._sample_rate = sample_rate
        self._params = params
        self._handler_fnc: Optional[Callable[..., Coroutine]] = None
        self._ep: Optional[AudioStreamEndpoint] = None
        self._active_sessions: dict[str, asyncio.Task] = {}

    @property
    def endpoint(self) -> Optional[AudioStreamEndpoint]:
        """The underlying Rust AudioStreamEndpoint, or None if not started."""
        return self._ep

    def handler(self) -> Callable:
        """Decorator to register the bot handler function.

        The handler receives an AudioStreamTransport and should build + run the pipeline::

            @server.handler()
            async def run_bot(transport: AudioStreamTransport):
                ...
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

        self._ep = AudioStreamEndpoint(
            listen_addr=self._listen_addr,
            plivo_auth_id=self._plivo_auth_id,
            plivo_auth_token=self._plivo_auth_token,
            sample_rate=self._sample_rate,
        )
        logger.info("AudioStream server listening on %s", self._listen_addr)

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
                params=self._params or TransportParams(
                    audio_in_enabled=True,
                    audio_out_enabled=True,
                ),
            )

            task = asyncio.create_task(self._run_session(session_id, transport))
            self._active_sessions[session_id] = task

    async def _run_session(self, session_id: str, transport: AudioStreamTransport) -> None:
        try:
            await self._handler_fnc(transport)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Session %s handler failed", session_id)
        finally:
            self._active_sessions.pop(session_id, None)
            logger.info("Session %s ended", session_id)
