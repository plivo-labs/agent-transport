"""LiveKit Agents adapters for agent-transport."""

import atexit
import aiohttp
from livekit.agents.utils import http_context

from .sip_io import SipAudioInput, SipAudioOutput
from .audio_stream_io import AudioStreamInput, AudioStreamOutput


def _ensure_http_context():
    """Set up the HTTP session context if not already in a LiveKit job context.

    LiveKit plugins like Deepgram and ElevenLabs use http_context.http_session()
    for their HTTP connections. Inside a LiveKit worker, this is set up automatically.
    When using agent-transport standalone, we set it up here so plugins work
    transparently without users needing to pass http_session kwargs.
    """
    if http_context._ContextVar.get(None) is not None:
        return  # already set up (e.g. inside a LiveKit worker)

    _session = None

    def _factory():
        nonlocal _session
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit_per_host=50, keepalive_timeout=120),
            )
        return _session

    http_context._ContextVar.set(_factory)

    # #19: Register cleanup to close the session on process exit
    def _cleanup():
        nonlocal _session
        if _session is not None and not _session.closed:
            # Schedule close on the event loop if it's still running
            try:
                loop = _session._loop  # aiohttp stores the loop
                if loop and loop.is_running():
                    loop.create_task(_session.close())
                elif loop and not loop.is_closed():
                    loop.run_until_complete(_session.close())
            except Exception:
                pass  # best-effort cleanup

    atexit.register(_cleanup)


# Set up HTTP context on import so all LiveKit plugins work standalone
_ensure_http_context()


from .server import AgentServer, CallContext, run_app

__all__ = [
    "SipAudioInput", "SipAudioOutput",
    "AudioStreamInput", "AudioStreamOutput",
    "AgentServer", "CallContext", "run_app",
]
