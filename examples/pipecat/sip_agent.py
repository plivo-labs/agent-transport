#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "agent-transport[pipecat]",
#     "python-dotenv",
#     "loguru",
#     "pipecat-ai[deepgram,openai,silero]",
# ]
# ///
"""Pipecat voice agent over SIP via agent-transport."""

import os

from dotenv import load_dotenv
from loguru import logger

from agent_transport.sip.pipecat import (
    SipServerTransport, AudioRecorder, SoundfileMixer,
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

server = SipServerTransport()


@server.setup()
def prewarm():
    """Load models once — shared across all calls."""
    return {"vad": SileroVADAnalyzer(), "turn": LocalSmartTurnAnalyzerV3()}


@server.handler()
async def run_bot(transport, userdata):
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
            vad_analyzer=userdata["vad"],
            user_turn_strategies=UserTurnStrategies(
                stop=[TurnAnalyzerUserTurnStopStrategy(
                    turn_analyzer=userdata["turn"],
                )],
            ),
        ),
    )

    # Rust-backed recorder: stereo OGG/Opus recording at the transport layer
    os.makedirs("/tmp/agent-sessions", exist_ok=True)
    recorder = AudioRecorder(
        transport,
        path=f"/tmp/agent-sessions/recording_{transport.session_id}.ogg",
        num_channels=2,
    )

    @recorder.event_handler("on_recording_stopped")
    async def on_recording_stopped(recorder, path):
        logger.info(f"Recording saved to {path}")

    # Background audio mixer (requires audio files on disk)
    # mixer = SoundfileMixer(transport, sound_files={"hold": "hold_music.wav"},
    #                        default_sound="hold", volume=0.3)

    pipeline = Pipeline([
        transport.input(),
        stt,
        user_aggregator,
        llm,
        tts,
        transport.output(),
        assistant_aggregator,
        recorder,
    ])

    task = PipelineTask(pipeline, params=PipelineParams(
        audio_in_sample_rate=8000,
        allow_interruptions=True,
        enable_metrics=True,
        enable_usage_metrics=True,
        # audio_out_mixer=mixer,
    ))

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport):
        await recorder.start_recording()
        logger.info(f"Call connected: {transport.remote_uri} ({transport.direction})")
        context.add_message({"role": "user", "content": "Please introduce yourself."})
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport):
        await task.cancel()

    runner = PipelineRunner()
    await runner.run(task)


if __name__ == "__main__":
    server.run()
