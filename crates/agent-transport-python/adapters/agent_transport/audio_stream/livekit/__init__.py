"""LiveKit Agents adapters for Plivo audio streaming transport.

Usage:
    from agent_transport.audio_stream.livekit import AudioStreamServer, JobContext, JobProcess

    server = AudioStreamServer(listen_addr="0.0.0.0:8765")

    def prewarm(proc: JobProcess):
        proc.userdata["vad"] = silero.VAD.load()

    server.setup_fnc = prewarm

    @server.audio_stream_session()
    async def entrypoint(ctx: JobContext):
        session = AgentSession(vad=ctx.proc.userdata["vad"], ...)
        ctx.session = session
        await session.start(agent=MyAgent(), room=ctx.room)

    server.run()
"""

# Re-export from sip.livekit — shared infrastructure, separate import path
from agent_transport.sip.livekit.audio_stream_server import (
    AudioStreamServer,
    JobContext,
)
from agent_transport.sip.livekit.audio_stream_io import (
    AudioStreamInput,
    AudioStreamOutput,
)
from agent_transport.sip.livekit.server import JobProcess
from agent_transport.sip.livekit._room_facade import TransportRoom

__all__ = [
    "AudioStreamServer",
    "JobContext",
    "JobProcess",
    "AudioStreamInput",
    "AudioStreamOutput",
    "TransportRoom",
]
