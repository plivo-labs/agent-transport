#!/usr/bin/env python3
"""Pipecat multi-agent over Plivo audio streaming.

Greeter → Sales/Support handoff via function calling.
Matches LiveKit multi-agent pattern adapted for Pipecat pipelines.

Prerequisites:
    pip install "pipecat-ai[deepgram,openai,silero]" python-dotenv loguru
"""

import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv
from loguru import logger

from agent_transport.audio_stream.pipecat.serializers.plivo import PlivoFrameSerializer
from agent_transport.audio_stream.pipecat.transports.websocket import WebsocketServerTransport
from agent_transport.audio_stream.pipecat.processors import AudioRecorder

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.tts import OpenAITTSService

load_dotenv()

# ─── Agent definitions ───────────────────────────────────────────────────────

GREETER_PROMPT = """\
You are a friendly receptionist. Greet the caller and determine their intent.
Ask if they need sales or support. When determined, call the appropriate
transfer function. Do not try to answer sales or support questions yourself."""

SALES_PROMPT = """\
You are a sales specialist. The caller was transferred from the receptionist.
Help with pricing, product information, and purchasing. Keep responses brief.
If the caller needs technical support, call transfer_to_support."""

SUPPORT_PROMPT = """\
You are a technical support specialist. The caller was transferred.
Help troubleshoot issues and answer technical questions. Keep responses brief.
If the caller wants to purchase something, call transfer_to_sales."""

SALES_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "transfer_to_support",
            "description": "Transfer caller to technical support",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

SUPPORT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "transfer_to_sales",
            "description": "Transfer caller to sales",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

GREETER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "transfer_to_sales",
            "description": "Transfer caller to sales department",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transfer_to_support",
            "description": "Transfer caller to support department",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


@dataclass
class AgentConfig:
    system_prompt: str
    tools: list = field(default_factory=list)
    voice: str = "alloy"
    greeting: Optional[str] = None


AGENTS = {
    "greeter": AgentConfig(
        system_prompt=GREETER_PROMPT,
        tools=GREETER_TOOLS,
        greeting="Hello! Welcome. How can I help you today — sales or support?",
    ),
    "sales": AgentConfig(
        system_prompt=SALES_PROMPT,
        tools=SALES_TOOLS,
        voice="nova",
        greeting="Hi, I'm from the sales team. How can I help you?",
    ),
    "support": AgentConfig(
        system_prompt=SUPPORT_PROMPT,
        tools=SUPPORT_TOOLS,
        voice="echo",
        greeting="Hi, I'm from support. What issue can I help you with?",
    ),
}


# ─── Server setup ────────────────────────────────────────────────────────────

serializer = PlivoFrameSerializer()
server = WebsocketServerTransport(serializer=serializer)


@server.setup()
def prewarm():
    """Load heavy models once — shared across all sessions."""
    return {"vad": SileroVADAnalyzer()}


@server.handler()
async def run_bot(transport, userdata):
    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    current_agent = "greeter"

    recorder = AudioRecorder(transport, path=f"/tmp/call-{transport.session_id}.ogg", num_channels=2)

    def make_pipeline(agent_name: str):
        """Build pipeline for current agent."""
        nonlocal current_agent
        current_agent = agent_name
        cfg = AGENTS[agent_name]

        llm = OpenAILLMService(
            api_key=os.getenv("OPENAI_API_KEY"),
            model="gpt-4o-mini",
            settings=OpenAILLMService.Settings(system_instruction=cfg.system_prompt),
        )
        tts = OpenAITTSService(api_key=os.getenv("OPENAI_API_KEY"), voice=cfg.voice)

        context = LLMContext()
        user_agg, asst_agg = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(vad_analyzer=userdata["vad"]),
        )

        pipeline = Pipeline([
            transport.input(), stt, user_agg, llm, tts,
            transport.output(), asst_agg, recorder,
        ])

        task = PipelineTask(pipeline, params=PipelineParams(
            audio_in_sample_rate=16000,
            audio_out_sample_rate=16000,
            allow_interruptions=True,
        ))

        return context, task

    # Start with greeter
    context, task = make_pipeline("greeter")

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport):
        await recorder.start_recording()
        cfg = AGENTS[current_agent]
        if cfg.greeting:
            context.add_message({"role": "assistant", "content": cfg.greeting})
        context.add_message({"role": "user", "content": "Please introduce yourself."})
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport):
        await task.cancel()

    runner = PipelineRunner()
    await runner.run(task)


if __name__ == "__main__":
    server.run()
