#!/usr/bin/env python3
"""Pipecat multi-agent over SIP via agent-transport.

Greeter → Sales/Support handoff via function calling.
Matches LiveKit multi-agent pattern adapted for Pipecat pipelines.

Prerequisites:
    pip install "pipecat-ai[deepgram,openai,silero]" python-dotenv loguru
"""

import asyncio
import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv
from loguru import logger

from agent_transport import SipEndpoint
from agent_transport.sip.pipecat import SipTransport

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
from pipecat.transports.base_transport import TransportParams

load_dotenv()

# ─── Agent definitions ───────────────────────────────────────────────────────

GREETER_PROMPT = """\
You are a friendly receptionist. Greet the caller and determine their intent.
Ask if they need sales or support. When determined, call the appropriate
transfer function. Do not try to answer sales or support questions yourself."""

SALES_PROMPT = """\
You are a sales specialist. Help with pricing, products, and purchasing.
Keep responses brief. If they need tech help, call transfer_to_support."""

SUPPORT_PROMPT = """\
You are a support specialist. Help troubleshoot issues and answer technical
questions. Keep responses brief. If they want to buy, call transfer_to_sales."""

GREETER_TOOLS = [
    {"type": "function", "function": {"name": "transfer_to_sales",
     "description": "Transfer to sales", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "transfer_to_support",
     "description": "Transfer to support", "parameters": {"type": "object", "properties": {}}}},
]

SALES_TOOLS = [
    {"type": "function", "function": {"name": "transfer_to_support",
     "description": "Transfer to support", "parameters": {"type": "object", "properties": {}}}},
]

SUPPORT_TOOLS = [
    {"type": "function", "function": {"name": "transfer_to_sales",
     "description": "Transfer to sales", "parameters": {"type": "object", "properties": {}}}},
]


@dataclass
class AgentConfig:
    system_prompt: str
    tools: list = field(default_factory=list)
    voice: str = "alloy"
    greeting: Optional[str] = None


AGENTS = {
    "greeter": AgentConfig(
        system_prompt=GREETER_PROMPT, tools=GREETER_TOOLS,
        greeting="Hello! Welcome. How can I help — sales or support?",
    ),
    "sales": AgentConfig(
        system_prompt=SALES_PROMPT, tools=SALES_TOOLS, voice="nova",
        greeting="Hi, I'm from sales. How can I help?",
    ),
    "support": AgentConfig(
        system_prompt=SUPPORT_PROMPT, tools=SUPPORT_TOOLS, voice="echo",
        greeting="Hi, I'm from support. What issue can I help with?",
    ),
}

# Load VAD once — shared across all calls
vad = SileroVADAnalyzer()


# ─── Bot logic ───────────────────────────────────────────────────────────────

async def run_bot(ep: SipEndpoint, call_id: str):
    transport = SipTransport(ep, call_id, params=TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ))

    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    current_agent = "greeter"

    def make_pipeline(agent_name: str):
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
            user_params=LLMUserAggregatorParams(vad_analyzer=vad),
        )

        pipeline = Pipeline([
            transport.input(), stt, user_agg, llm, tts,
            transport.output(), asst_agg,
        ])

        task = PipelineTask(pipeline, params=PipelineParams(
            audio_in_sample_rate=16000,
            audio_out_sample_rate=16000,
            allow_interruptions=True,
        ))

        return context, task

    context, task = make_pipeline("greeter")

    context.add_message({"role": "user", "content": "Please introduce yourself."})
    await task.queue_frames([LLMRunFrame()])

    runner = PipelineRunner()
    await runner.run(task)


# ─── Main loop ───────────────────────────────────────────────────────────────

async def main():
    ep = SipEndpoint(
        sip_server=os.getenv("SIP_DOMAIN", "phone.plivo.com"),
        log_level=3,
    )

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, lambda: ep.register(os.getenv("SIP_USERNAME", ""), os.getenv("SIP_PASSWORD", ""))
    )

    event = await loop.run_in_executor(None, lambda: ep.wait_for_event(timeout_ms=10000))
    if not event or event["type"] != "registered":
        logger.error(f"Registration failed: {event}")
        return
    logger.info("Registered, waiting for calls...")

    while True:
        event = await loop.run_in_executor(None, lambda: ep.wait_for_event(timeout_ms=1000))
        if not event or event["type"] != "incoming_call":
            continue

        call_id = event["session"]["call_id"]
        logger.info(f"Incoming call from {event['session']['remote_uri']}")
        await loop.run_in_executor(None, lambda: ep.answer(call_id))

        while True:
            ev = await loop.run_in_executor(None, lambda: ep.wait_for_event(timeout_ms=500))
            if ev and ev["type"] == "call_media_active":
                break

        asyncio.create_task(run_bot(ep, call_id))


if __name__ == "__main__":
    asyncio.run(main())
