"""Rust-backed pipeline processors for Pipecat Plivo audio stream transport.

Thin re-export of the shared, transport-agnostic implementation in
``agent_transport._pipecat_processors``. Kept so existing imports
(``from agent_transport.audio_stream.pipecat import AudioRecorder`` /
``from .processors import AudioRecorder``) keep working.
"""

from agent_transport._pipecat_processors import AudioRecorder

__all__ = ["AudioRecorder"]
