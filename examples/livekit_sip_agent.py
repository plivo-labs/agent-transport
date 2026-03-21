#!/usr/bin/env python3
"""LiveKit voice agent over SIP transport.

Registers with a SIP server, waits for incoming calls, and runs
a LiveKit VoicePipelineAgent (STT → LLM → TTS) over the SIP call.

No LiveKit server or WebRTC needed — audio flows directly over RTP.

Prerequisites:
    cd crates/agent-transport-python && maturin develop
    cd python && pip install -e ".[livekit]"
    pip install livekit-agents livekit-plugins-deepgram livekit-plugins-openai

Usage:
    SIP_USERNAME=xxx SIP_PASSWORD=yyy \
    DEEPGRAM_API_KEY=xxx OPENAI_API_KEY=xxx \
    python examples/livekit_sip_agent.py
"""

import asyncio
import logging
import os

from agent_transport import SipEndpoint
from agent_transport_adapters.livekit import SipAudioInput, SipAudioOutput

from livekit.agents.voice import AgentSession, Agent, ModelSettings
from livekit.plugins import deepgram, openai

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PhoneAgent(Agent):
    """Simple conversational agent for phone calls."""

    def __init__(self):
        super().__init__(
            instructions="You are a helpful phone assistant. Keep responses brief and natural.",
        )


async def handle_call(ep: SipEndpoint, call_id: int):
    """Run the voice pipeline for a single call."""
    audio_input = SipAudioInput(ep, call_id)
    audio_output = SipAudioOutput(ep, call_id)

    session = AgentSession(
        stt=deepgram.STT(),
        llm=openai.LLM(model="gpt-4o-mini"),
        tts=openai.TTS(voice="alloy"),
    )

    # Set our SIP transport as the I/O — AgentSession will skip RoomIO
    session.input.audio = audio_input
    session.output.audio = audio_output

    logger.info("Starting voice pipeline for call %d", call_id)
    await session.start(agent=PhoneAgent(), capture_run=True)
    logger.info("Call %d ended", call_id)


async def main():
    username = os.environ.get("SIP_USERNAME", "")
    password = os.environ.get("SIP_PASSWORD", "")
    sip_domain = os.environ.get("SIP_DOMAIN", "phone.plivo.com")

    ep = SipEndpoint(sip_server=sip_domain, log_level=3)

    loop = asyncio.get_running_loop()

    # Register
    await loop.run_in_executor(None, lambda: ep.register(username, password))
    event = await loop.run_in_executor(None, lambda: ep.wait_for_event(timeout_ms=10000))
    if not event or event["type"] != "registered":
        logger.error("Registration failed: %s", event)
        return
    logger.info("Registered as %s@%s", username, sip_domain)

    # Accept calls in a loop
    logger.info("Waiting for incoming calls...")
    try:
        while True:
            event = await loop.run_in_executor(None, lambda: ep.wait_for_event(timeout_ms=1000))
            if not event:
                continue
            if event["type"] == "incoming_call":
                call_id = event["session"]["call_id"]
                logger.info("Incoming call from %s", event["session"]["remote_uri"])
                await loop.run_in_executor(None, lambda: ep.answer(call_id))

                # Wait for media
                while True:
                    ev = await loop.run_in_executor(None, lambda: ep.wait_for_event(timeout_ms=500))
                    if ev and ev["type"] == "call_media_active":
                        break

                # Run pipeline (blocks until call ends)
                await handle_call(ep, call_id)
    except KeyboardInterrupt:
        pass

    ep.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
