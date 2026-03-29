"""Pipecat adapters for Plivo audio stream transport (Rust-backed).

Module structure mirrors pipecat's layout:
    agent_transport.audio_stream.pipecat.serializers.plivo   → PlivoFrameSerializer
    agent_transport.audio_stream.pipecat.transports.websocket → WebsocketServerTransport
    agent_transport.audio_stream.pipecat.mixers               → SoundfileMixer (Rust-backed)
"""

from .serializers.plivo import PlivoFrameSerializer
from .transports.websocket import WebsocketServerTransport, WebsocketServerParams
from .audio_stream_transport import (
    AudioStreamTransport,
    AudioStreamInputTransport,
    AudioStreamOutputTransport,
)
from .mixers import SoundfileMixer

__all__ = [
    "PlivoFrameSerializer",
    "WebsocketServerTransport",
    "WebsocketServerParams",
    "AudioStreamTransport",
    "AudioStreamInputTransport",
    "AudioStreamOutputTransport",
    "SoundfileMixer",
]
