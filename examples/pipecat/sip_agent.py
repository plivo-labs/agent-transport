#!/usr/bin/env python3
"""Pipecat voice agent over SIP via agent-transport.

Prerequisites:
    pip install "pipecat-ai[deepgram,openai,silero]" python-dotenv loguru
"""

import asyncio
import os

from dotenv import load_dotenv
from loguru import logger

from agent_transport import SipEndpoint
from agent_transport.sip.pipecat import SipTransport

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transports.base_transport import TransportParams

load_dotenv()

# Load models once at module level — shared across all calls
# ONNX runtime caches compiled models, so subsequent sessions are fast
vad = SileroVADAnalyzer()
turn_detector = LocalSmartTurnAnalyzerV3()


async def run_bot(ep: SipEndpoint, call_id: str):
    transport = SipTransport(ep, call_id, params=TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
    ))

    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        settings=OpenAILLMService.Settings(
            system_instruction=(
                "You are a helpful voice assistant on a phone call. "
                "Your output will be converted to audio so don't include special characters. "
                "Respond in short, conversational sentences."
            ),
        ),
    )

    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    tts = OpenAITTSService(api_key=os.getenv("OPENAI_API_KEY"))

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=vad,
            user_turn_strategies=UserTurnStrategies(
                stop=[TurnAnalyzerUserTurnStopStrategy(
                    turn_analyzer=turn_detector,
                )],
            ),
        ),
    )

    pipeline = Pipeline([
        transport.input(),
        stt,
        user_aggregator,
        llm,
        tts,
        transport.output(),
        assistant_aggregator,
    ])

    task = PipelineTask(pipeline, params=PipelineParams(
        audio_in_sample_rate=16000,
        audio_out_sample_rate=16000,
        allow_interruptions=True,
        enable_metrics=True,
        enable_usage_metrics=True,
    ))

    context.add_message({"role": "user", "content": "Please introduce yourself."})
    await task.queue_frames([LLMRunFrame()])

    runner = PipelineRunner()
    await runner.run(task)


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
