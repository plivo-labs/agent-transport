# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "agent-transport[livekit]",
#     "python-dotenv",
#     "livekit-plugins-deepgram",
#     "livekit-plugins-openai",
#     "livekit-plugins-silero",
#     "livekit-plugins-turn-detector",
# ]
# ///
"""Audio streaming multi-agent with handoff and tool calling.

Demonstrates multiple agents that can hand off to each other over
Plivo audio streaming (same pattern as sip_multi_agent.py):
- GreeterAgent: greets the caller, gathers intent, hands off
- SalesAgent: handles product inquiries with tool calling
- SupportAgent: handles support issues with tool calling

Each agent can have its own instructions and tools. Returning an Agent
from a function tool triggers automatic handoff.

Setup:
    Configure Plivo XML answer URL to return:
    <Response>
        <Stream bidirectional="true" keepCallAlive="true"
            contentType="audio/x-mulaw;rate=8000">
            wss://your-server:8765
        </Stream>
    </Response>

Usage:
    uv run examples/livekit/audio_stream_multi_agent.py start
    uv run examples/livekit/audio_stream_multi_agent.py dev
"""

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv

from agent_transport.audio_stream.livekit import AudioStreamServer, JobContext, JobProcess

from livekit.agents import Agent, AgentSession, RunContext, TurnHandlingOptions, metrics

from livekit.agents.llm import function_tool
from livekit.agents.voice.background_audio import BackgroundAudioPlayer, BuiltinAudioClip
from livekit.agents.job import get_job_context
from livekit.plugins import deepgram, openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

load_dotenv()

logger = logging.getLogger("audio-stream-multi-agent")

server = AudioStreamServer(
    listen_addr=os.environ.get("AUDIO_STREAM_ADDR", "0.0.0.0:8765"),
    plivo_auth_id=os.environ.get("PLIVO_AUTH_ID", ""),
    plivo_auth_token=os.environ.get("PLIVO_AUTH_TOKEN", ""),
)


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["turn_detector"] = MultilingualModel()


server.setup_fnc = prewarm


@dataclass
class CallData:
    """Shared data across agents — passed via RunContext.userdata."""
    caller_name: str | None = None
    intent: str | None = None


class GreeterAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are a friendly receptionist. Your job is to greet the caller, "
                "ask for their name, and determine if they need sales or support. "
                "The caller can also press 1 for sales or 2 for support on their keypad. "
                "Keep responses brief and natural. "
                "Do not use emojis, asterisks, markdown, or special formatting."
            ),
        )

    async def on_enter(self) -> None:
        job_ctx = get_job_context()
        job_ctx.room.on("sip_dtmf_received", self._on_dtmf)
        self.session.generate_reply(
            instructions="Greet the caller and ask how you can help them today. "
            "Let them know they can press 1 for sales or 2 for support."
        )

    async def on_exit(self) -> None:
        pass

    def _on_dtmf(self, ev) -> None:
        logger.info("DTMF received: %s", ev.digit)
        if ev.digit == "1":
            self.session.generate_reply(
                instructions="The caller pressed 1 for sales. Ask for their name and route them to sales."
            )
        elif ev.digit == "2":
            self.session.generate_reply(
                instructions="The caller pressed 2 for support. Ask for their name and route them to support."
            )

    @function_tool
    async def route_to_sales(
        self, context: RunContext[CallData], caller_name: str
    ) -> Agent:
        """Route the caller to the sales team when they are interested in
        purchasing a product or learning about pricing.

        Args:
            caller_name: The caller's name
        """
        context.userdata.caller_name = caller_name
        context.userdata.intent = "sales"
        logger.info("Routing %s to sales", caller_name)
        return SalesAgent(caller_name)

    @function_tool
    async def route_to_support(
        self, context: RunContext[CallData], caller_name: str
    ) -> Agent:
        """Route the caller to the support team when they have an issue
        or need help with an existing product.

        Args:
            caller_name: The caller's name
        """
        context.userdata.caller_name = caller_name
        context.userdata.intent = "support"
        logger.info("Routing %s to support", caller_name)
        return SupportAgent(caller_name)


class SalesAgent(Agent):
    def __init__(self, caller_name: str) -> None:
        super().__init__(
            instructions=(
                f"You are a sales agent speaking with {caller_name}. "
                "Help them learn about products and pricing. "
                "Be enthusiastic but not pushy. Keep responses concise. "
                "Do not use emojis, asterisks, markdown, or special formatting."
            ),
        )
        self._caller_name = caller_name

    async def on_enter(self) -> None:
        self.session.generate_reply(
            instructions=f"Introduce yourself as a sales specialist to {self._caller_name} "
            "and ask what product they're interested in."
        )

    @function_tool
    async def check_pricing(
        self, context: RunContext[CallData], product: str
    ) -> str:
        """Look up pricing for a product.

        Args:
            product: The product name to check pricing for
        """
        logger.info("Checking pricing for %s", product)
        return f"{product} starts at $49/month for the basic plan and $99/month for premium."

    @function_tool
    async def check_availability(
        self, context: RunContext[CallData], product: str
    ) -> str:
        """Check if a product is available.

        Args:
            product: The product to check availability for
        """
        logger.info("Checking availability for %s", product)
        return f"{product} is available and can be set up within 24 hours."

    @function_tool
    async def transfer_to_support(self, context: RunContext[CallData]) -> Agent:
        """Transfer to support if the caller has an existing issue instead of a sales inquiry."""
        logger.info("Sales transferring %s to support", self._caller_name)
        return SupportAgent(self._caller_name)

    @function_tool
    async def end_call(self, context: RunContext[CallData]) -> str:
        """End the call when the conversation is complete and the user is done."""
        logger.info("Sales ending call with %s", self._caller_name)
        context.session.shutdown()
        return "Say goodbye to the user."


class SupportAgent(Agent):
    def __init__(self, caller_name: str) -> None:
        super().__init__(
            instructions=(
                f"You are a support agent speaking with {caller_name}. "
                "Help them resolve their issue. Be empathetic and solution-oriented. "
                "Keep responses concise. "
                "Do not use emojis, asterisks, markdown, or special formatting."
            ),
        )
        self._caller_name = caller_name

    async def on_enter(self) -> None:
        self.session.generate_reply(
            instructions=f"Introduce yourself as a support specialist to {self._caller_name} "
            "and ask them to describe their issue."
        )

    @function_tool
    async def lookup_account(
        self, context: RunContext[CallData], account_id: str
    ) -> str:
        """Look up a customer's account details.

        Args:
            account_id: The customer's account ID or email
        """
        logger.info("Looking up account %s", account_id)
        return f"Account {account_id} is active, on the premium plan, last payment was 15 days ago."

    @function_tool
    async def create_ticket(
        self, context: RunContext[CallData], issue_description: str
    ) -> str:
        """Create a support ticket for the caller's issue.

        Args:
            issue_description: Description of the issue
        """
        logger.info("Creating ticket: %s", issue_description)
        return "Support ticket #12345 has been created. Our team will follow up within 2 hours."

    @function_tool
    async def transfer_to_sales(self, context: RunContext[CallData]) -> Agent:
        """Transfer to sales if the caller wants to upgrade or purchase."""
        logger.info("Support transferring %s to sales", self._caller_name)
        return SalesAgent(self._caller_name)

    @function_tool
    async def end_call(self, context: RunContext[CallData]) -> str:
        """End the call when the issue is resolved and the user is done."""
        logger.info("Support ending call with %s", self._caller_name)
        context.session.shutdown()
        return "Say goodbye to the user."


@server.audio_stream_session()
async def entrypoint(ctx: JobContext):
    session = AgentSession[CallData](
        vad=ctx.proc.userdata["vad"],
        stt=deepgram.STT(model="nova-3"),
        llm=openai.LLM(model="gpt-4.1-mini"),
        tts=openai.TTS(voice="alloy"),
        userdata=CallData(),
        turn_handling=TurnHandlingOptions(
            turn_detection=ctx.proc.userdata["turn_detector"],
        ),
        preemptive_generation=True,
        aec_warmup_duration=3.0,
        tts_text_transforms=["filter_emoji", "filter_markdown"],
    )
    ctx.session = session

    bg_audio = BackgroundAudioPlayer(
        ambient_sound=BuiltinAudioClip.OFFICE_AMBIENCE,
        thinking_sound=BuiltinAudioClip.KEYBOARD_TYPING,
    )
    await bg_audio.start(room=ctx.room, agent_session=session)

    await session.start(agent=GreeterAgent(), room=ctx.room)


if __name__ == "__main__":
    server.run()
