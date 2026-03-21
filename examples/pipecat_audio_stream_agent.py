#!/usr/bin/env python3
"""Pipecat voice agent over Plivo audio streaming.

Starts a WebSocket server that Plivo connects to. Runs a Pipecat
pipeline (STT → LLM → TTS) over the Plivo audio stream.

No SIP registration needed — Plivo initiates the WebSocket connection.

Prerequisites:
    cd crates/agent-transport-python && maturin develop --features audio-stream
    cd python && pip install -e ".[pipecat]"
    pip install "pipecat-ai[deepgram,openai]"

Setup:
    Configure Plivo XML answer URL to return:
    <Response>
        <Stream bidirectional="true" url="ws://your-server:8080" />
    </Response>

Usage:
    PLIVO_AUTH_ID=xxx PLIVO_AUTH_TOKEN=yyy \
    DEEPGRAM_API_KEY=xxx OPENAI_API_KEY=xxx \
    python examples/pipecat_audio_stream_agent.py
"""

import asyncio
import logging
import os

from agent_transport import AudioStreamEndpoint
from agent_transport_adapters.pipecat import AudioStreamTransport

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.services.deepgram import DeepgramSTTService
from pipecat.services.openai import OpenAILLMService, OpenAITTSService
from pipecat.transports.base_transport import TransportParams

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def handle_session(ep, session_id: int):
    """Run the Pipecat pipeline for a single audio stream session."""
    transport = AudioStreamTransport(ep, session_id, params=TransportParams(
        audio_in_enabled=True,
        audio_in_passthrough=True,
        audio_out_enabled=True,
        audio_in_sample_rate=16000,
        audio_out_sample_rate=16000,
    ))

    stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"])
    llm = OpenAILLMService(
        api_key=os.environ["OPENAI_API_KEY"],
        model="gpt-4o-mini",
        params=OpenAILLMService.InputParams(
            temperature=0.7,
        ),
    )
    tts = OpenAITTSService(
        api_key=os.environ["OPENAI_API_KEY"],
        voice="alloy",
    )

    pipeline = Pipeline([
        transport.input(),
        stt,
        llm,
        tts,
        transport.output(),
    ])

    task = PipelineTask(pipeline, params=PipelineParams(
        allow_interruptions=True,
    ))

    runner = PipelineRunner()
    logger.info("Pipeline started for session %d", session_id)
    await runner.run(task)
    logger.info("Pipeline finished for session %d", session_id)


async def main():
    ep = AudioStreamEndpoint(
        listen_addr="0.0.0.0:8080",
        plivo_auth_id=os.environ.get("PLIVO_AUTH_ID", ""),
        plivo_auth_token=os.environ.get("PLIVO_AUTH_TOKEN", ""),
    )
    logger.info("WebSocket server listening on 0.0.0.0:8080")

    loop = asyncio.get_running_loop()

    try:
        while True:
            logger.info("Waiting for Plivo to connect...")
            event = await loop.run_in_executor(None, lambda: ep.wait_for_event(timeout_ms=0))
            if not event or event["type"] != "incoming_call":
                continue

            session_id = event["session"]["call_id"]
            logger.info("Plivo connected — session %d", session_id)

            # Wait for media active
            await loop.run_in_executor(None, lambda: ep.wait_for_event(timeout_ms=5000))

            await handle_session(ep, session_id)
    except KeyboardInterrupt:
        pass

    ep.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
