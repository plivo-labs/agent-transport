#!/usr/bin/env python3
"""Example: LiveKit Agent with SIP transport.

Registers with a SIP server, receives incoming calls, and connects
them to a LiveKit VoicePipelineAgent with STT/LLM/TTS.

Prerequisites:
    cd crates/agent-transport-python && maturin develop
    pip install agent-transport-adapters[livekit]
    pip install livekit-plugins-deepgram livekit-plugins-openai livekit-plugins-elevenlabs

Usage:
    SIP_USERNAME=xxx SIP_PASSWORD=yyy python examples/livekit_sip_agent.py
"""

import asyncio
import os

from agent_transport import SipEndpoint, EndpointConfig
from agent_transport_adapters.livekit import SipAudioInput, SipAudioOutput

# Uncomment when livekit-agents is installed:
# from livekit.agents import AgentSession
# from livekit.agents.voice import VoicePipelineAgent
# from livekit.plugins import deepgram, openai, elevenlabs


async def main():
    username = os.environ.get("SIP_USERNAME", "")
    password = os.environ.get("SIP_PASSWORD", "")
    sip_domain = os.environ.get("SIP_DOMAIN", "phone.plivo.com")

    # Initialize SIP endpoint
    ep = SipEndpoint(sip_server=sip_domain, log_level=3)
    ep.register(username, password)

    event = ep.wait_for_event(timeout_ms=10000)
    if event is None or event["type"] != "registered":
        print(f"Registration failed: {event}")
        return
    print(f"Registered as {username}@{sip_domain}")

    # Wait for incoming call
    print("Waiting for incoming call...")
    while True:
        event = ep.wait_for_event(timeout_ms=1000)
        if event and event["type"] == "incoming_call":
            call_id = event["session"].call_id
            print(f"Incoming call from {event['session'].remote_uri}")
            ep.answer(call_id)
            break

    # Wait for media
    while True:
        event = ep.wait_for_event(timeout_ms=500)
        if event and event["type"] == "call_media_active":
            break

    print("Call connected. Audio bridge active.")

    # Create LiveKit I/O adapters
    audio_input = SipAudioInput(ep, call_id)
    audio_output = SipAudioOutput(ep, call_id)

    # In production, connect to a VoicePipelineAgent:
    # agent = VoicePipelineAgent(
    #     stt=deepgram.STT(),
    #     llm=openai.LLM(),
    #     tts=elevenlabs.TTS(),
    # )
    # session = AgentSession()
    # session.input.audio = audio_input
    # session.output.audio = audio_output
    # await session.start(agent=agent)

    # For now, just echo audio (loopback)
    print("Loopback mode: echoing audio. Press Ctrl+C to hang up.")
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
