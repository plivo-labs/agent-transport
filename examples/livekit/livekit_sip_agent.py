"""SIP voice agent — handles inbound and outbound calls.

Inbound:  SIP INVITE arrives → agent answers and starts conversation
Outbound: POST /call {"to": "sip:+15551234567@provider.com"}

Usage:
    python examples/livekit_sip_agent.py start       # production
    python examples/livekit_sip_agent.py dev         # dev mode
    python examples/livekit_sip_agent.py debug       # full debug
"""

import os

from dotenv import load_dotenv

from agent_transport.sip.livekit import AgentServer, CallContext

from livekit.agents import Agent, AgentSession
from livekit.plugins import deepgram, openai, silero

load_dotenv()


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are a helpful phone assistant. "
                "Keep responses concise and conversational. "
                "Do not use emojis, markdown, or special formatting."
            ),
        )

    async def on_enter(self) -> None:
        self.session.generate_reply(
            instructions="Greet the user and ask how you can help."
        )


server = AgentServer(
    sip_username=os.environ["SIP_USERNAME"],
    sip_password=os.environ["SIP_PASSWORD"],
    sip_server=os.environ.get("SIP_DOMAIN", "phone.plivo.com"),
)


@server.sip_session()
async def entrypoint(ctx: CallContext):
    session = AgentSession(
        vad=silero.VAD.load(),
        stt=deepgram.STT(model="nova-3"),
        llm=openai.LLM(model="gpt-4.1-mini"),
        tts=openai.TTS(voice="alloy"),
    )
    await ctx.start(session, agent=Assistant())


if __name__ == "__main__":
    server.run()
