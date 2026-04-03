"""Audio streaming voice agent with tool calling and DTMF support.

Plivo connects to your WebSocket server and streams audio bidirectionally.
No SIP credentials needed — configure Plivo XML to point to your server.

Uses the same LiveKit Agents patterns as WebRTC — get_job_context().room works,
DTMF events come through room.on("sip_dtmf_received"), built-in tools like
send_dtmf_events work out of the box.

Setup:
    Configure Plivo XML answer URL to return:
    <Response>
        <Stream bidirectional="true" keepCallAlive="true"
            contentType="audio/x-mulaw;rate=8000">
            wss://your-server:8765
        </Stream>
    </Response>

Usage:
    python examples/livekit/audio_stream_agent.py start       # production
    python examples/livekit/audio_stream_agent.py dev         # dev mode
    python examples/livekit/audio_stream_agent.py debug       # full debug
"""

import logging
import os

from dotenv import load_dotenv

from agent_transport.audio_stream.livekit import AudioStreamServer, JobContext, JobProcess

from livekit.agents import Agent, AgentSession, RunContext, TurnHandlingOptions, metrics

from livekit.agents.llm import function_tool
from livekit.agents.voice.background_audio import BackgroundAudioPlayer, BuiltinAudioClip
from livekit.agents.job import get_job_context
from livekit.agents.beta.tools import send_dtmf_events
from livekit.plugins import deepgram, openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

load_dotenv()

logger = logging.getLogger("audio-stream-agent")

server = AudioStreamServer(
    listen_addr=os.environ.get("AUDIO_STREAM_ADDR", "0.0.0.0:8765"),
    plivo_auth_id=os.environ.get("PLIVO_AUTH_ID", ""),
    plivo_auth_token=os.environ.get("PLIVO_AUTH_TOKEN", ""),
)


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["turn_detector"] = MultilingualModel()


server.setup_fnc = prewarm


class Assistant(Agent):
    """Voice agent with tool calling and DTMF support.

    Same Agent class works with both SIP and audio streaming —
    just swap AudioStreamServer for AgentServer.
    """

    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are a helpful phone assistant. "
                "Keep responses concise and conversational. "
                "Do not use emojis, asterisks, markdown, or special formatting."
            ),
            # LiveKit's built-in send_dtmf_events tool — works via Room facade
            tools=[send_dtmf_events],
        )

    async def on_enter(self) -> None:
        job_ctx = get_job_context()
        job_ctx.room.on("sip_dtmf_received", self._on_dtmf)
        self.session.generate_reply(
            instructions="Greet the user and ask how you can help."
        )

    async def on_exit(self) -> None:
        pass

    def _on_dtmf(self, ev) -> None:
        logger.info("DTMF received: digit=%s code=%d", ev.digit, ev.code)

    @function_tool
    async def lookup_weather(
        self, context: RunContext, location: str, latitude: str, longitude: str
    ) -> str:
        """Called when the user asks for weather related information.
        Ensure the user's location (city or region) is provided.
        When given a location, please estimate the latitude and longitude
        and do not ask the user for them.

        Args:
            location: The location they are asking for
            latitude: The latitude of the location, do not ask user for it
            longitude: The longitude of the location, do not ask user for it
        """
        logger.info("Looking up weather for %s", location)
        return f"The weather in {location} is sunny with a temperature of 72 degrees."

    @function_tool
    async def end_call(self, context: RunContext) -> str:
        """Ends the current call and disconnects.

        Call when:
        - The user clearly indicates they are done (e.g., "that's all, bye")
        - The conversation is complete and should end

        Do not call when:
        - The user asks to pause, hold, or transfer
        - Intent is unclear
        """
        logger.info("End call requested")
        context.session.shutdown()
        return "Say goodbye to the user."


@server.audio_stream_session()
async def entrypoint(ctx: JobContext):
    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=deepgram.STT(model="nova-3"),
        llm=openai.LLM(model="gpt-4.1-mini"),
        tts=openai.TTS(voice="alloy"),
        turn_handling=TurnHandlingOptions(
            turn_detection=ctx.proc.userdata["turn_detector"],
        ),
        preemptive_generation=True,
        aec_warmup_duration=3.0,
        tts_text_transforms=["filter_emoji", "filter_markdown"],
    )
    # Same pattern as LiveKit WebRTC: session.start(agent=, room=ctx.room)
    ctx.session = session

    # Background audio — ambient plays continuously, thinking plays while agent processes
    bg_audio = BackgroundAudioPlayer(
        ambient_sound=BuiltinAudioClip.OFFICE_AMBIENCE,
        thinking_sound=BuiltinAudioClip.KEYBOARD_TYPING,
    )
    await bg_audio.start(room=ctx.room, agent_session=session)

    await session.start(agent=Assistant(), room=ctx.room)


if __name__ == "__main__":
    server.run()
