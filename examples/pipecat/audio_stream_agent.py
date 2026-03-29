#!/usr/bin/env python3
"""Pipecat voice agent over Plivo audio streaming.

Prerequisites:
    pip install "pipecat-ai[deepgram,openai,silero]" python-dotenv loguru soundfile
"""

import os

from dotenv import load_dotenv
from loguru import logger

from agent_transport.audio_stream.pipecat.serializers.plivo import PlivoFrameSerializer
from agent_transport.audio_stream.pipecat.transports.websocket import WebsocketServerTransport
from agent_transport.audio_stream.pipecat.mixers import SoundfileMixer
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
from pipecat.transports.base_transport import TransportParams

load_dotenv()

serializer = PlivoFrameSerializer()
server = WebsocketServerTransport(serializer=serializer)


@server.handler()
async def run_bot(transport):
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
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    # Rust-backed background audio mixer (optional)
    # Feeds audio to Rust's send loop — zero GIL mixing overhead
    # mixer = SoundfileMixer(
    #     transport,
    #     sound_files={"hold": "hold_music.wav"},
    #     default_sound="hold",
    #     volume=0.3,
    # )

    # Rust-backed call recorder (optional)
    # Records directly in Rust's 20ms send loop — OGG/Opus, stereo (L=user, R=agent)
    recorder = AudioRecorder(transport, f"/tmp/call-{transport.session_id}.ogg")

    @recorder.event_handler("on_recording_stopped")
    async def on_recording_stopped(recorder, path):
        logger.info(f"Recording saved to {path}")

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
        audio_in_sample_rate=16000,
        audio_out_sample_rate=16000,
        allow_interruptions=True,
        enable_metrics=True,
        enable_usage_metrics=True,
        # audio_out_mixer=mixer,  # uncomment to enable background audio
    ))

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport):
        context.add_message({"role": "user", "content": "Please introduce yourself."})
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport):
        await task.cancel()

    runner = PipelineRunner()
    await runner.run(task)


if __name__ == "__main__":
    server.run()
