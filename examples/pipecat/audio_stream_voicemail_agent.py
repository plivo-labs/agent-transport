#!/usr/bin/env python3
"""Pipecat voicemail agent over Plivo audio streaming — beep detection.

Detects voicemail beep on incoming audio, leaves a message.
If no beep (human answered), starts a conversation instead.

Prerequisites:
    pip install "pipecat-ai[deepgram,openai,silero]" python-dotenv loguru
"""

import os

from dotenv import load_dotenv
from loguru import logger

from agent_transport.audio_stream.pipecat import (
    PlivoFrameSerializer, WebsocketServerTransport, AudioRecorder,
)

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

load_dotenv()

VOICEMAIL_MESSAGE = (
    "Hi, this is a message from Acme Corp. "
    "We're calling about your recent inquiry. "
    "Please call us back at your convenience. Thank you!"
)

HUMAN_GREETING = (
    "You are a friendly sales representative from Acme Corp calling about "
    "a recent inquiry. Be brief and conversational."
)

serializer = PlivoFrameSerializer()
server = WebsocketServerTransport(serializer=serializer)


@server.setup()
def prewarm():
    return {"vad": SileroVADAnalyzer(), "turn": LocalSmartTurnAnalyzerV3()}


@server.handler()
async def run_bot(transport, userdata):
    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    tts = OpenAITTSService(api_key=os.getenv("OPENAI_API_KEY"))

    context = LLMContext()
    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        settings=OpenAILLMService.Settings(system_instruction=HUMAN_GREETING),
    )

    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=userdata["vad"],
            user_turn_strategies=UserTurnStrategies(
                stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=userdata["turn"])],
            ),
        ),
    )

    recorder = AudioRecorder(transport, path=f"/tmp/call-{transport.session_id}.ogg", num_channels=2)

    pipeline = Pipeline([
        transport.input(), stt, user_aggregator, llm, tts,
        transport.output(), assistant_aggregator, recorder,
    ])

    task = PipelineTask(pipeline, params=PipelineParams(
        audio_in_sample_rate=16000,
        audio_out_sample_rate=16000,
        allow_interruptions=True,
    ))

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport):
        await recorder.start_recording()
        # Start beep detection — wait up to 30s for voicemail beep
        transport.detect_beep(timeout_ms=30000)
        logger.info("Stream connected, detecting beep...")

    @transport.event_handler("on_beep_detected")
    async def on_beep_detected(transport, frequency_hz, duration_ms):
        logger.info(f"Beep detected ({frequency_hz:.0f}Hz, {duration_ms}ms) — leaving voicemail")
        context.add_message({"role": "user", "content": f"Say exactly: {VOICEMAIL_MESSAGE}"})
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_beep_timeout")
    async def on_beep_timeout(transport):
        logger.info("No beep detected — human answered, starting conversation")
        context.add_message({"role": "user", "content": "Greet the person and introduce yourself."})
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport):
        await task.cancel()

    runner = PipelineRunner()
    await runner.run(task)


if __name__ == "__main__":
    server.run()
