#!/usr/bin/env python3
"""Pipecat agent with SIP transport.

Registers with a SIP server, receives incoming calls, and connects
audio to a Pipecat pipeline with STT/LLM/TTS.

Prerequisites:
    cd crates/agent-transport-python && maturin develop
    cd python && pip install -e ".[pipecat]"

Usage:
    SIP_USERNAME=xxx SIP_PASSWORD=yyy python examples/pipecat_sip_agent.py
"""

import asyncio
import os

from agent_transport import SipEndpoint
from agent_transport_adapters.pipecat import SipTransport

# Uncomment with pipecat-ai installed:
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

    loop = asyncio.get_running_loop()
    event = await loop.run_in_executor(None, lambda: ep.wait_for_event(timeout_ms=10000))
    if not event or event["type"] != "registered":
        print(f"Registration failed: {event}")
        return
    print(f"Registered as {username}@{sip_domain}")

    print("Waiting for incoming call...")
    while True:
        event = await loop.run_in_executor(None, lambda: ep.wait_for_event(timeout_ms=1000))
        if event and event["type"] == "incoming_call":
            call_id = event["session"]["call_id"]
            print(f"Incoming call from {event['session']['remote_uri']}")
            ep.answer(call_id)
            break

    while True:
        event = await loop.run_in_executor(None, lambda: ep.wait_for_event(timeout_ms=500))
        if event and event["type"] == "call_media_active":
            break

    print("Call connected.")

    # Create Pipecat transport
    transport = SipTransport(ep, call_id)

    # Build pipeline:
    # pipeline = Pipeline([
    #     transport.input(),
    #     DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"]),
    #     OpenAILLMService(api_key=os.environ["OPENAI_API_KEY"]),
    #     ElevenLabsTTSService(api_key=os.environ["ELEVENLABS_API_KEY"]),
    #     transport.output(),
    # ])
    # runner = PipelineRunner()
    # await runner.run(pipeline)

    # Loopback
    print("Loopback mode — echoing audio. Ctrl+C to hang up.")
    try:
        while True:
            result = await loop.run_in_executor(
                None, lambda: ep.recv_audio_bytes_blocking(call_id, 20)
            )
            if result is not None:
                audio, sr, nc = result
                ep.send_audio_bytes(call_id, audio, sr, nc)
    except KeyboardInterrupt:
        pass

    ep.hangup(call_id)
    ep.shutdown()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
