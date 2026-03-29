"""Rust-backed Plivo audio streaming serializer.

Drop-in replacement for pipecat.serializers.plivo.PlivoFrameSerializer.
Codec negotiation, audio encoding/decoding, and Plivo protocol handling
are done in Rust — this class holds the configuration.

Usage:
    from agent_transport.audio_stream.pipecat.serializers.plivo import PlivoFrameSerializer
    from agent_transport.audio_stream.pipecat.transports.websocket import WebsocketServerTransport

    serializer = PlivoFrameSerializer(auth_id="...", auth_token="...")
    transport = WebsocketServerTransport(serializer=serializer)
"""

import os
from typing import Optional


class PlivoFrameSerializer:
    """Plivo audio streaming configuration.

    Matches pipecat.serializers.plivo.PlivoFrameSerializer interface.
    Serialization is handled in Rust (20ms paced audio, codec negotiation,
    clearAudio, checkpoint, sendDTMF). This class holds the config passed
    to the Rust AudioStreamEndpoint.
    """

    def __init__(
        self,
        *,
        auth_id: Optional[str] = None,
        auth_token: Optional[str] = None,
        listen_addr: Optional[str] = None,
        sample_rate: int = 8000,
        auto_hangup: bool = True,
    ):
        self.auth_id = auth_id or os.environ.get("PLIVO_AUTH_ID", "")
        self.auth_token = auth_token or os.environ.get("PLIVO_AUTH_TOKEN", "")
        self.listen_addr = listen_addr or os.environ.get("AUDIO_STREAM_ADDR", "0.0.0.0:8080")
        self.sample_rate = sample_rate
        self.auto_hangup = auto_hangup
