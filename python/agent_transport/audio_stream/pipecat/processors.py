"""Rust-backed pipeline processors for Pipecat.

Drop-in alternatives to Pipecat's AudioBufferProcessor that delegate
recording to Rust for zero-copy, low-overhead call recording.

Usage:
    from agent_transport.audio_stream.pipecat.processors import AudioRecorder

    recorder = AudioRecorder(transport, "/tmp/call.ogg")
    pipeline = Pipeline([transport.input(), stt, llm, tts, transport.output(), recorder])
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from pipecat.frames.frames import (
        CancelFrame, EndFrame, Frame, StartFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
except ImportError:
    raise ImportError("pipecat-ai is required: pip install pipecat-ai")


class AudioRecorder(FrameProcessor):
    """Rust-backed call recorder as a Pipecat FrameProcessor.

    Drop-in pipeline processor that records the call using Rust's transport-level
    recording. Records directly in the 20ms send loop — zero Python audio
    buffering, zero GIL overhead.

    Place after transport.output() in the pipeline (same position as
    AudioBufferProcessor). Recording starts on StartFrame and stops on
    EndFrame/CancelFrame.

    Output format: OGG/Opus. Stereo mode: L=user audio, R=agent audio.

    Usage:
        recorder = AudioRecorder(transport, "/tmp/call.ogg")
        pipeline = Pipeline([
            transport.input(), stt, llm, tts,
            transport.output(), recorder,
        ])

        # Optional: get notified when recording stops
        @recorder.event_handler("on_recording_stopped")
        async def on_stopped(recorder, path):
            logger.info("Recording saved to %s", path)
    """

    def __init__(
        self,
        transport: "AudioStreamTransport",
        path: str,
        *,
        stereo: bool = True,
        auto_start: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._transport = transport
        self._path = path
        self._stereo = stereo
        self._auto_start = auto_start
        self._recording = False
        self._register_event_handler("on_recording_stopped")

    async def start_recording(self, path: Optional[str] = None):
        """Start recording. Called automatically on StartFrame if auto_start=True."""
        p = path or self._path
        if not self._recording:
            self._transport.start_recording(p, self._stereo)
            self._recording = True
            logger.info("Recording started: %s", p)

    async def stop_recording(self):
        """Stop recording. Called automatically on EndFrame/CancelFrame."""
        if self._recording:
            self._transport.stop_recording()
            self._recording = False
            logger.info("Recording stopped: %s", self._path)
            await self._call_event_handler("on_recording_stopped", self, self._path)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, StartFrame) and self._auto_start:
            await self.start_recording()
        elif isinstance(frame, (EndFrame, CancelFrame)):
            await self.stop_recording()

        await self.push_frame(frame, direction)
