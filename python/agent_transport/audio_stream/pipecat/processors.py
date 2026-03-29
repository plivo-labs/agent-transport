"""Rust-backed pipeline processors for Pipecat.

AudioRecorder subclasses Pipecat's AudioBufferProcessor — inherits all
Python-level callbacks (on_audio_data, on_track_audio_data, per-turn events)
and adds Rust transport-level file recording on top.

Usage — minimal change from AudioBufferProcessor:

    # Before (Pipecat)
    from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
    recorder = AudioBufferProcessor(num_channels=2)

    # After (Agent Transport) — add transport arg, optionally enable file recording
    from agent_transport.audio_stream.pipecat.processors import AudioRecorder
    recorder = AudioRecorder(transport, num_channels=2, path="/tmp/call.ogg")

All AudioBufferProcessor events work identically:
    on_audio_data, on_track_audio_data, on_user_turn_audio_data, on_bot_turn_audio_data
"""

import logging
import os
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from pipecat.frames.frames import CancelFrame, EndFrame, Frame, StartFrame
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
    - Records directly in Rust's 20ms send loop (zero Python overhead)
    - OGG/Opus output, stereo (L=user, R=agent)
    - on_recording_stopped(path) event when file is written

    If path is not provided, behaves exactly like AudioBufferProcessor
    (no file recording, just callbacks).
    """

    def __init__(
        self,
        transport: "AudioStreamTransport",
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
        self._register_event_handler("on_recording_stopped")

    async def start_recording(self):
        """Start recording. Starts both Python buffering and Rust file recording."""
        await super().start_recording()
        if self._path:
            try:
                self._transport.start_recording(self._path, self._stereo)
                self._rust_recording = True
            except Exception as e:
                logger.warning("Rust recording failed to start: %s", e)

    async def stop_recording(self):
        """Stop recording. Stops Rust file, then fires Python callbacks."""
        # Stop Rust recording first (while session may still exist)
        if self._rust_recording:
            try:
                self._transport.stop_recording()
            except Exception as e:
                logger.warning("Rust recording failed to stop: %s", e)
            self._rust_recording = False
        # Fire Python callbacks (on_audio_data, on_track_audio_data)
        await super().stop_recording()
        # Notify file path
        if self._path:
            await self._call_event_handler("on_recording_stopped", self._path)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        # Stop Rust recording before EndFrame/CancelFrame propagates to transport
        # (transport.output().stop() removes session via hangup)
        if isinstance(frame, (EndFrame, CancelFrame)) and self._rust_recording:
            try:
                self._transport.stop_recording()
            except Exception as e:
                logger.debug("AudioRecorder stop_recording error: %s", e)
            self._rust_recording = False

        await super().process_frame(frame, direction)
