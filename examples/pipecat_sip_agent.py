#!/usr/bin/env python3
"""Pipecat voice agent over SIP transport.

Registers with a SIP server, waits for incoming calls, and runs
a Pipecat pipeline (STT → LLM → TTS) over the SIP call.

No WebSocket server needed — audio flows directly over RTP.

Prerequisites:
    cd crates/agent-transport-python && maturin develop
    cd python && pip install -e ".[pipecat]"
    pip install "pipecat-ai[deepgram,openai]"

Usage:
    SIP_USERNAME=xxx SIP_PASSWORD=yyy \
    DEEPGRAM_API_KEY=xxx OPENAI_API_KEY=xxx \
    python examples/pipecat_sip_agent.py
"""

import asyncio
import logging
import os
import sys

from agent_transport import SipEndpoint
from agent_transport_adapters.pipecat import SipTransport

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.services.deepgram import DeepgramSTTService
from pipecat.services.openai import OpenAILLMService, OpenAITTSService
from pipecat.transports.base_transport import TransportParams

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def handle_call(ep: SipEndpoint, call_id: int):
    """Run the Pipecat pipeline for a single call."""
    transport = SipTransport(ep, call_id, params=TransportParams(
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
    logger.info("Pipeline started for call %d", call_id)
    await runner.run(task)
    logger.info("Pipeline finished for call %d", call_id)


async def main():
    username = os.environ.get("SIP_USERNAME", "")
    password = os.environ.get("SIP_PASSWORD", "")
    sip_domain = os.environ.get("SIP_DOMAIN", "phone.plivo.com")

    ep = SipEndpoint(sip_server=sip_domain, log_level=3)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: ep.register(username, password))
    event = await loop.run_in_executor(None, lambda: ep.wait_for_event(timeout_ms=10000))
    if not event or event["type"] != "registered":
        logger.error("Registration failed: %s", event)
        return
    logger.info("Registered as %s@%s", username, sip_domain)

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

                while True:
                    ev = await loop.run_in_executor(None, lambda: ep.wait_for_event(timeout_ms=500))
                    if ev and ev["type"] == "call_media_active":
                        break

                await handle_call(ep, call_id)
    except KeyboardInterrupt:
        pass

    ep.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
