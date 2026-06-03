"""Rust-backed pipeline processors for Pipecat (shared SIP + audio_stream impl).

AudioRecorder subclasses Pipecat's AudioBufferProcessor — inherits all
Python-level callbacks (on_audio_data, on_track_audio_data, per-turn events)
and adds Rust transport-level file recording on top.

This module is transport-agnostic: it works against any object exposing
``start_recording(path, stereo)`` / ``stop_recording()`` (SipTransport or
AudioStreamTransport). The package-specific ``sip/pipecat/processors.py`` and
``audio_stream/pipecat/processors.py`` are thin re-exports of this module.

Usage — minimal change from AudioBufferProcessor:

    # Before (Pipecat)
    from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
    recorder = AudioBufferProcessor(num_channels=2)

    # After (Agent Transport) — add transport arg, optionally enable file recording
    from agent_transport.sip.pipecat import AudioRecorder            # SIP
    from agent_transport.audio_stream.pipecat import AudioRecorder   # Plivo
    recorder = AudioRecorder(transport, num_channels=2,
                             path=f"/tmp/agent-sessions/recording_{transport.session_id}.ogg")

All AudioBufferProcessor events work identically:
    on_audio_data, on_track_audio_data, on_user_turn_audio_data, on_bot_turn_audio_data
"""

from typing import Any, Optional

from loguru import logger

try:
    from pipecat.frames.frames import CancelFrame, EndFrame, Frame
    from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
    from pipecat.processors.frame_processor import FrameDirection
except ImportError:
    raise ImportError("pipecat-ai is required: pip install pipecat-ai")


class AudioRecorder(AudioBufferProcessor):
    """AudioBufferProcessor + Rust file recording.

    Inherits all AudioBufferProcessor behavior:
    - Python-level audio buffering and merging
    - on_audio_data(merged_audio, sample_rate, num_channels)
    - on_track_audio_data(user_audio, bot_audio, sample_rate, num_channels)
    - on_user_turn_audio_data(turn_audio, sample_rate, 1)
    - on_bot_turn_audio_data(turn_audio, sample_rate, 1)

    Adds Rust transport-level file recording:
    - Records directly in Rust's send loop (zero Python overhead)
    - OGG/Opus output, stereo (L=user, R=agent)
    - on_recording_stopped(path) event when file is written

    If path is not provided, behaves exactly like AudioBufferProcessor
    (no file recording, just callbacks).
    """

    def __init__(
        self,
        transport: Any,
        *,
        path: Optional[str] = None,
        stereo: bool = True,
        sample_rate: Optional[int] = None,
        num_channels: int = 1,
        buffer_size: int = 0,
        enable_turn_audio: bool = False,
        **kwargs,
    ):
        super().__init__(
            sample_rate=sample_rate,
            num_channels=num_channels,
            buffer_size=buffer_size,
            enable_turn_audio=enable_turn_audio,
            **kwargs,
        )
        self._transport = transport
        self._path = path
        self._stereo = stereo
        self._rust_recording = False
        # Tracks whether stop_recording() has already fired for the current
        # recording, so EndFrame/CancelFrame and an explicit stop_recording()
        # don't double-fire on_recording_stopped. Reset in start_recording()
        # so a single recorder instance can be restarted.
        self._stop_fired = False
        self._register_event_handler("on_recording_stopped")

    async def start_recording(self):
        """Start recording. Starts both Python buffering and Rust file recording."""
        self._stop_fired = False
        await super().start_recording()
        if self._path:
            try:
                self._transport.start_recording(self._path, self._stereo)
                self._rust_recording = True
            except Exception as e:
                logger.warning("Rust recording failed to start: {}", e)

    async def stop_recording(self):
        """Stop recording. Stops Rust file, then fires Python callbacks."""
        if not self._path or self._stop_fired:
            return
        self._stop_fired = True
        if self._rust_recording:
            try:
                self._transport.stop_recording()
            except Exception as e:
                logger.warning("Rust recording failed to stop: {}", e)
            self._rust_recording = False
        await super().stop_recording()
        await self._call_event_handler("on_recording_stopped", self._path)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        # Stop Rust recording before EndFrame/CancelFrame propagates to transport
        # (transport.output().stop() removes session via hangup).
        if isinstance(frame, (EndFrame, CancelFrame)) and self._rust_recording:
            try:
                self._transport.stop_recording()
            except Exception as e:
                logger.debug("AudioRecorder stop_recording error: {}", e)
            self._rust_recording = False

        await super().process_frame(frame, direction)
