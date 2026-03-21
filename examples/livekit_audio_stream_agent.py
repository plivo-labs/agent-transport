#!/usr/bin/env python3
"""LiveKit agent with Plivo audio streaming transport.

Starts a WebSocket server that Plivo connects to for bidirectional audio,
then bridges it to a LiveKit VoicePipelineAgent.

Prerequisites:
    cd crates/agent-transport-python && maturin develop --features audio-stream
    cd python && pip install -e ".[livekit]"

Setup:
    1. Configure Plivo XML with: <Stream bidirectional="true" url="ws://your-server:8080" />
    2. Set PLIVO_AUTH_ID and PLIVO_AUTH_TOKEN env vars

Usage:
    PLIVO_AUTH_ID=xxx PLIVO_AUTH_TOKEN=yyy python examples/livekit_audio_stream_agent.py
"""

import asyncio
import os

from agent_transport import AudioStreamEndpoint
from agent_transport_adapters.livekit import AudioStreamInput, AudioStreamOutput

# Uncomment with livekit-agents installed:
# from livekit.agents import AgentSession
# from livekit.agents.voice import VoicePipelineAgent
# from livekit.plugins import deepgram, openai, elevenlabs


async def main():
    ep = AudioStreamEndpoint(
        listen_addr="0.0.0.0:8080",
        plivo_auth_id=os.environ.get("PLIVO_AUTH_ID", ""),
        plivo_auth_token=os.environ.get("PLIVO_AUTH_TOKEN", ""),
    )
    print("WebSocket server listening on 0.0.0.0:8080")
    print("Waiting for Plivo to connect...")

    loop = asyncio.get_running_loop()
    event = await loop.run_in_executor(None, lambda: ep.wait_for_event(timeout_ms=60000))
    if not event or event["type"] != "incoming_call":
        print(f"No connection: {event}")
        return

    session_id = event["session"]["call_id"]
    print(f"Plivo connected — session {session_id}")

    # Wait for media active
    await loop.run_in_executor(None, lambda: ep.wait_for_event(timeout_ms=5000))

    # Create LiveKit I/O
    audio_input = AudioStreamInput(ep, session_id)
    audio_output = AudioStreamOutput(ep, session_id)

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
    print("Loopback mode — echoing audio. Ctrl+C to stop.")
    try:
        async for frame in audio_input:
            await loop.run_in_executor(
                None, lambda f=frame: ep.send_audio_bytes(session_id, bytes(f.data), f.sample_rate, f.num_channels)
            )
    except (KeyboardInterrupt, StopAsyncIteration):
        pass

    ep.hangup(session_id)
    ep.shutdown()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
