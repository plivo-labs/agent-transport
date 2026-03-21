#!/usr/bin/env python3
"""LiveKit agent with SIP transport.

Registers with a SIP server, receives incoming calls, and bridges
audio to a LiveKit VoicePipelineAgent with STT/LLM/TTS.

Prerequisites:
    cd crates/agent-transport-python && maturin develop
    cd python && pip install -e ".[livekit]"

Usage:
    SIP_USERNAME=xxx SIP_PASSWORD=yyy python examples/livekit_sip_agent.py
"""

import asyncio
import os

from agent_transport import SipEndpoint
from agent_transport_adapters.livekit import SipAudioInput, SipAudioOutput

# Uncomment with livekit-agents installed:
# from livekit.agents import AgentSession
# from livekit.agents.voice import VoicePipelineAgent
# from livekit.plugins import deepgram, openai, elevenlabs


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

    # Wait for media
    while True:
        event = await loop.run_in_executor(None, lambda: ep.wait_for_event(timeout_ms=500))
        if event and event["type"] == "call_media_active":
            break

    print("Call connected.")

    # Create LiveKit I/O
    audio_input = SipAudioInput(ep, call_id)
    audio_output = SipAudioOutput(ep, call_id)

    # Connect to VoicePipelineAgent:
    # agent = VoicePipelineAgent(
    #     stt=deepgram.STT(),
    #     llm=openai.LLM(),
    #     tts=elevenlabs.TTS(),
    # )
    # session = AgentSession()
    # session.input.audio = audio_input
    # session.output.audio = audio_output
    # await session.start(agent=agent)

    # Loopback
    print("Loopback mode — echoing audio. Ctrl+C to hang up.")
    try:
        async for frame in audio_input:
            await loop.run_in_executor(
                None, lambda f=frame: ep.send_audio_bytes(call_id, bytes(f.data), f.sample_rate, f.num_channels)
            )
    except (KeyboardInterrupt, StopAsyncIteration):
        pass

    ep.hangup(call_id)
    ep.shutdown()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
