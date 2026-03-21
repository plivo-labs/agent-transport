#!/usr/bin/env python3
"""LiveKit voice agent over Plivo audio streaming.

Starts a WebSocket server that Plivo connects to. Runs a LiveKit
VoicePipelineAgent (STT → LLM → TTS) over the Plivo audio stream.

No LiveKit server or WebRTC needed — audio flows over Plivo WebSocket.

Prerequisites:
    cd crates/agent-transport-python && maturin develop --features audio-stream
    cd python && pip install -e ".[livekit]"
    pip install livekit-agents livekit-plugins-deepgram livekit-plugins-openai

Setup:
    Configure Plivo XML answer URL to return:
    <Response>
        <Stream bidirectional="true" url="ws://your-server:8080" />
    </Response>

Usage:
    PLIVO_AUTH_ID=xxx PLIVO_AUTH_TOKEN=yyy \
    DEEPGRAM_API_KEY=xxx OPENAI_API_KEY=xxx \
    python examples/livekit_audio_stream_agent.py
"""

import asyncio
import logging
import os

from agent_transport import AudioStreamEndpoint
from agent_transport_adapters.livekit import AudioStreamInput, AudioStreamOutput

from livekit.agents.voice import AgentSession, Agent
from livekit.plugins import deepgram, openai

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PhoneAgent(Agent):
    def __init__(self):
        super().__init__(
            instructions="You are a helpful phone assistant. Keep responses brief and natural.",
        )


async def handle_session(ep, session_id: int):
    """Run the voice pipeline for a single audio stream session."""
    audio_input = AudioStreamInput(ep, session_id)
    audio_output = AudioStreamOutput(ep, session_id)

    session = AgentSession(
        stt=deepgram.STT(),
        llm=openai.LLM(model="gpt-4o-mini"),
        tts=openai.TTS(voice="alloy"),
    )

    session.input.audio = audio_input
    session.output.audio = audio_output

    logger.info("Starting voice pipeline for session %d", session_id)
    await session.start(agent=PhoneAgent(), capture_run=True)
    logger.info("Session %d ended", session_id)


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

            # Run pipeline (blocks until stream ends)
            await handle_session(ep, session_id)
    except KeyboardInterrupt:
        pass

    ep.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
