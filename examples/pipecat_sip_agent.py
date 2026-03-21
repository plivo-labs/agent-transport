#!/usr/bin/env python3
"""Example: Pipecat Agent with SIP transport.

Registers with a SIP server, receives incoming calls, and connects
them to a Pipecat pipeline with STT/LLM/TTS.

Prerequisites:
    cd crates/agent-transport-python && maturin develop
    pip install agent-transport-adapters[pipecat]
    pip install pipecat-ai[deepgram,openai,elevenlabs]

Usage:
    SIP_USERNAME=xxx SIP_PASSWORD=yyy python examples/pipecat_sip_agent.py
"""

import asyncio
import os

from agent_transport import SipEndpoint
from agent_transport_adapters.pipecat import SipTransport

# Uncomment when pipecat-ai is installed:
# from pipecat.pipeline.pipeline import Pipeline
# from pipecat.pipeline.runner import PipelineRunner
# from pipecat.services.deepgram import DeepgramSTTService
# from pipecat.services.openai import OpenAILLMService
# from pipecat.services.elevenlabs import ElevenLabsTTSService


async def main():
    username = os.environ.get("SIP_USERNAME", "")
    password = os.environ.get("SIP_PASSWORD", "")
    sip_domain = os.environ.get("SIP_DOMAIN", "phone.plivo.com")

    ep = SipEndpoint(sip_server=sip_domain, log_level=3)
    ep.register(username, password)

    event = ep.wait_for_event(timeout_ms=10000)
    if not event or event["type"] != "registered":
        print(f"Registration failed: {event}")
        return
    print(f"Registered as {username}@{sip_domain}")

    print("Waiting for incoming call...")
    while True:
        event = ep.wait_for_event(timeout_ms=1000)
        if event and event["type"] == "incoming_call":
            call_id = event["session"].call_id
            print(f"Incoming call from {event['session'].remote_uri}")
            ep.answer(call_id)
            break

    while True:
        event = ep.wait_for_event(timeout_ms=500)
        if event and event["type"] == "call_media_active":
            break

    print("Call connected.")

    # Create Pipecat transport
    transport = SipTransport(ep, call_id)

    # In production, build a pipeline:
    # pipeline = Pipeline([
    #     transport.input(),
    #     DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"]),
    #     OpenAILLMService(api_key=os.environ["OPENAI_API_KEY"]),
    #     ElevenLabsTTSService(api_key=os.environ["ELEVENLABS_API_KEY"]),
    #     transport.output(),
    # ])
    # runner = PipelineRunner()
    # await runner.run(pipeline)

    # For now, loopback
    print("Loopback mode. Press Ctrl+C to hang up.")
    try:
        while True:
            frame = ep.recv_audio_blocking(call_id, 20)
            if frame:
                ep.send_audio(call_id, frame)
    except KeyboardInterrupt:
        pass

    ep.hangup(call_id)
    ep.shutdown()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
