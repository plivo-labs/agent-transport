"""Rust-backed pipeline processors for Pipecat SIP transport.

Thin re-export of the shared, transport-agnostic implementation in
``agent_transport._pipecat_processors``. Kept so existing imports
(``from agent_transport.sip.pipecat import AudioRecorder`` /
``from .processors import AudioRecorder``) keep working.
"""

from agent_transport._pipecat_processors import AudioRecorder

__all__ = ["AudioRecorder"]
