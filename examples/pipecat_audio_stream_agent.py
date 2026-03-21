#!/usr/bin/env python3
"""Pipecat agent with Plivo audio streaming transport.

Starts a WebSocket server that Plivo connects to for bidirectional audio.
No SIP registration needed — Plivo initiates the connection.

Prerequisites:
    cd crates/agent-transport-python && maturin develop --features audio-stream
    cd python && pip install -e ".[pipecat]"

Setup:
    1. Configure Plivo XML with: <Stream bidirectional="true" url="ws://your-server:8080" />
    2. Set PLIVO_AUTH_ID and PLIVO_AUTH_TOKEN env vars

Usage:
    PLIVO_AUTH_ID=xxx PLIVO_AUTH_TOKEN=yyy python examples/pipecat_audio_stream_agent.py
"""

import asyncio
import os

from agent_transport import AudioStreamEndpoint
from agent_transport_adapters.pipecat import AudioStreamTransport

# Uncomment with pipecat-ai installed:
# from pipecat.pipeline.pipeline import Pipeline
# from pipecat.pipeline.runner import PipelineRunner
# from pipecat.services.deepgram import DeepgramSTTService
# from pipecat.services.openai import OpenAILLMService
# from pipecat.services.elevenlabs import ElevenLabsTTSService


async def main():
    ep = AudioStreamEndpoint(
        listen_addr="0.0.0.0:8080",
        plivo_auth_id=os.environ.get("PLIVO_AUTH_ID", ""),
        plivo_auth_token=os.environ.get("PLIVO_AUTH_TOKEN", ""),
    )
    print("WebSocket server listening on 0.0.0.0:8080")
    print("Configure Plivo to stream audio here. Waiting for connection...")

    # Wait for Plivo to connect
    loop = asyncio.get_running_loop()
    event = await loop.run_in_executor(None, lambda: ep.wait_for_event(timeout_ms=60000))
    if not event or event["type"] != "incoming_call":
        print(f"No connection: {event}")
        return

    session_id = event["session"]["call_id"]
    print(f"Plivo connected — session {session_id}")

    # Wait for media active
    event = await loop.run_in_executor(None, lambda: ep.wait_for_event(timeout_ms=5000))

    # Create transport
    transport = AudioStreamTransport(ep, session_id)

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

    # Loopback until disconnect
    print("Loopback mode — echoing audio. Ctrl+C to stop.")
    try:
        while True:
            result = await loop.run_in_executor(
                None, lambda: ep.recv_audio_bytes_blocking(session_id, 20)
            )
            if result is not None:
                audio, sr, nc = result
                ep.send_audio_bytes(session_id, audio, sr, nc)
    except KeyboardInterrupt:
        pass

    ep.hangup(session_id)
    ep.shutdown()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
