"""LiveKit Agents AudioInput/AudioOutput adapters for Plivo audio streaming.

Architecture is a line-for-line match of LiveKit's _ParticipantAudioOutput
using SipAudioSource and Chan.
"""

import asyncio
import logging
import time
from typing import Optional

try:
    from livekit import rtc
    from livekit.agents import utils
    from livekit.agents.voice.io import AudioInput, AudioOutput
    try:
        from livekit.agents.voice.io import PlaybackStartedEvent, PlaybackFinishedEvent, AudioOutputCapabilities
    except ImportError:
        from dataclasses import dataclass
        @dataclass
        class PlaybackStartedEvent:
            created_at: float
        @dataclass
        class PlaybackFinishedEvent:
            playback_position: float
            interrupted: bool
            synchronized_transcript: Optional[str] = None
        @dataclass
        class AudioOutputCapabilities:
            pause: bool
except ImportError:
    raise ImportError("livekit-agents is required: pip install livekit-agents")

from .sip_io import _to_livekit_frame
from ._audio_source import AudioStreamAudioSource
from ._channel import Chan
from ._aio_utils import cancel_and_wait

logger = logging.getLogger(__name__)


# ─── AudioInput ───────────────────────────────────────────────────────────────

class AudioStreamInput(AudioInput):
    """Async iterator yielding AudioFrames from Plivo audio stream.

    Architecture matches _ParticipantAudioInputStream:
    - Forwarding task reads from Rust and pushes into Chan
    - __anext__ reads from Chan
    - On stream end, pushes 0.5s silence to flush STT, then closes Chan
    """

    def __init__(self, endpoint, session_id: str, *, label: str = "audio-stream-input", source=None, **kwargs):
        try:
            super().__init__(label=label, source=source)
        except TypeError:
            pass
        self._ep = endpoint
        self._sid = session_id
        self._label = label
        self._source = source
        self._sample_rate = endpoint.input_sample_rate
        self._num_channels = 1

        self._data_ch: Chan[rtc.AudioFrame] = Chan()
        self._forward_task: asyncio.Task[None] | None = None
        self._attached = True
        self._closed = False

    @property
    def label(self) -> str: return self._label
    @property
    def source(self): return self._source

    async def start(self) -> None:
        if self._forward_task is None:
            self._forward_task = asyncio.create_task(self._forward_audio())

    async def __anext__(self) -> rtc.AudioFrame:
        if self._source:
            return await self._source.__anext__()

        if self._forward_task is None:
            await self.start()

        return await self._data_ch.__anext__()

    def __aiter__(self): return self

    async def _forward_audio(self) -> None:
        loop = asyncio.get_running_loop()
        frame_count = 0
        try:
            while not self._closed:
                try:
                    result = await loop.run_in_executor(
                        None, lambda: self._ep.recv_audio_bytes_blocking(self._sid, 20) if not self._closed else None
                    )
                except Exception as e:
                    logger.debug("AudioStreamInput recv error: %s", e)
                    break
                if result is not None and self._attached:
                    ab, sr, nc = result
                    frame = _to_livekit_frame(bytes(ab), sr, nc)
                    await self._data_ch.send(frame)
                    frame_count += 1
                    if frame_count == 1:
                        logger.info("AudioStreamInput: first frame received sr=%d samples=%d", sr, frame.samples_per_channel)
                    elif frame_count % 250 == 0:  # every 5 seconds
                        logger.info("AudioStreamInput: %d frames forwarded (%.1fs)", frame_count, frame_count * 0.02)
        finally:
            silent_samples = int(self._sample_rate * 0.5)
            silence = rtc.AudioFrame(
                b"\x00\x00" * silent_samples,
                sample_rate=self._sample_rate,
                num_channels=self._num_channels,
                samples_per_channel=silent_samples,
            )
            try:
                await self._data_ch.send(silence)
            except Exception:
                pass

    def on_attached(self) -> None:
        self._attached = True
        if self._source: self._source.on_attached()

    def on_detached(self) -> None:
        self._attached = False
        if self._source: self._source.on_detached()

    async def aclose(self) -> None:
        self._closed = True
        if self._forward_task:
            await cancel_and_wait(self._forward_task)
        self._data_ch.close()

    def __repr__(self) -> str:
        return f"AudioStreamInput(label={self._label!r}, source={self._source!r})"


# ─── AudioOutput ──────────────────────────────────────────────────────────────

class AudioStreamOutput(AudioOutput):
    """Sends AudioFrames to Plivo audio stream.

    Line-for-line match of LiveKit's _ParticipantAudioOutput.
    """

    def __init__(
        self,
        endpoint,
        session_id: str,
        *,
        label: str = "audio-stream-output",
        sample_rate: Optional[int] = None,
        num_channels: int = 1,
        next_in_chain=None,
        **kwargs,
    ) -> None:
        _sample_rate = sample_rate or endpoint.output_sample_rate

        super().__init__(
            label=label,
            next_in_chain=None,
            sample_rate=_sample_rate,
            capabilities=AudioOutputCapabilities(pause=True),
        )

        self._ep = endpoint
        self._sid = session_id

        self._audio_source = AudioStreamAudioSource(
            endpoint, session_id,
            sample_rate=_sample_rate,
            num_channels=num_channels,
            queue_size_ms=200,  # matches _ParticipantAudioOutput production (not rtc.AudioSource default of 1000)
        )

        self._audio_buf: Chan[rtc.AudioFrame] = Chan()
        self._audio_bstream = utils.audio.AudioByteStream(
            _sample_rate, num_channels, samples_per_channel=_sample_rate // 20
        )

        self._flush_task: asyncio.Task[None] | None = None
        self._interrupted_event = asyncio.Event()
        self._forwarding_task: asyncio.Task[None] | None = None

        self._pushed_duration: float = 0.0

        self._playback_enabled = asyncio.Event()
        self._playback_enabled.set()
        self._first_frame_event = asyncio.Event()

        self._rust_paused = False

        self._ready = asyncio.Event()

    @property
    def sample_rate(self) -> int | None:
        return self._audio_source.sample_rate

    async def start(self) -> None:
        self._forwarding_task = asyncio.create_task(self._forward_audio())
        self._ready.set()

    async def aclose(self) -> None:
        if self._flush_task:
            await cancel_and_wait(self._flush_task)
        if self._forwarding_task:
            await cancel_and_wait(self._forwarding_task)

        await self._audio_source.aclose()

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        if self._forwarding_task is None:
            await self.start()

        await self._ready.wait()

        await super().capture_frame(frame)

        if self._flush_task and not self._flush_task.done():
            logger.error("capture_frame called while flush is in progress")
            await self._flush_task

        for f in self._audio_bstream.push(frame.data):
            try:
                self._audio_buf.send_nowait(f)
            except Exception:
                logger.warning("AudioStreamOutput: send_nowait failed in capture_frame, dropping frame")
                continue
            self._pushed_duration += f.duration

    def flush(self) -> None:
        super().flush()

        for f in self._audio_bstream.flush():
            try:
                self._audio_buf.send_nowait(f)
            except Exception:
                logger.warning("AudioStreamOutput: send_nowait failed in flush, dropping frame")
                continue
            self._pushed_duration += f.duration

        if not self._pushed_duration:
            logger.debug("flush: no pushed_duration, skipping")
            return

        if self._flush_task and not self._flush_task.done():
            logger.error("flush called while playback is in progress")
            self._flush_task.cancel()

        logger.debug("flush: pushed_dur=%.3fs, creating _wait_for_playout task", self._pushed_duration)
        self._flush_task = asyncio.create_task(self._wait_for_playout())

    def clear_buffer(self) -> None:
        logger.info("AudioStreamOutput.clear_buffer: clearing bstream, pushed_dur=%.3fs", self._pushed_duration)
        self._audio_bstream.clear()

        if not self._pushed_duration:
            logger.info("AudioStreamOutput.clear_buffer: no pushed_duration, skipping interrupt")
            return
        logger.info("AudioStreamOutput.clear_buffer: setting _interrupted_event")
        self._interrupted_event.set()

    def pause(self) -> None:
        super().pause()
        self._playback_enabled.clear()
        if not self._rust_paused:
            self._rust_paused = True
            try:
                self._ep.pause(self._sid)
            except Exception:
                pass

    def resume(self) -> None:
        super().resume()
        self._playback_enabled.set()
        self._first_frame_event.clear()
        if self._rust_paused:
            self._rust_paused = False
            try:
                self._ep.resume(self._sid)
            except Exception:
                pass

    async def _wait_for_playout(self) -> None:
        logger.debug("_wait_for_playout: starting (pushed=%.3fs)", self._pushed_duration)
        wait_for_interruption = asyncio.create_task(self._interrupted_event.wait())

        async def _wait_buffered_audio() -> None:
            while not self._audio_buf.empty():
                if not self._playback_enabled.is_set():
                    await self._playback_enabled.wait()
                logger.debug("_wait_buffered_audio: chan_qsize=%d, awaiting audio_source.wait_for_playout", self._audio_buf.qsize())
                await self._audio_source.wait_for_playout()
                await asyncio.sleep(0)
            logger.debug("_wait_buffered_audio: chan empty, playout done")

        wait_for_playout = asyncio.create_task(_wait_buffered_audio())
        await asyncio.wait(
            {wait_for_playout, wait_for_interruption},
            return_when=asyncio.FIRST_COMPLETED,
        )

        interrupted = wait_for_interruption.done()
        pushed_duration = self._pushed_duration

        if interrupted:
            queued_duration = self._audio_source.queued_duration
            while True:
                try:
                    queued_duration += self._audio_buf.recv_nowait().duration
                except Exception:
                    break
            pushed_duration = max(pushed_duration - queued_duration, 0)
            self._audio_source.clear_queue()
            wait_for_playout.cancel()
            logger.debug("_wait_for_playout: interrupted, played=%.3fs", pushed_duration)
        else:
            wait_for_interruption.cancel()
            logger.debug("_wait_for_playout: completed, played=%.3fs", pushed_duration)

        self._pushed_duration = 0
        self._interrupted_event.clear()
        self._first_frame_event.clear()
        self.on_playback_finished(playback_position=pushed_duration, interrupted=interrupted)

    async def _forward_audio(self) -> None:
        async for frame in self._audio_buf:
            if not self._playback_enabled.is_set():
                self._audio_source.clear_queue()
                await self._playback_enabled.wait()

            if self._interrupted_event.is_set() or self._pushed_duration == 0:
                if self._interrupted_event.is_set() and self._flush_task:
                    await self._flush_task

                continue

            if not self._first_frame_event.is_set():
                self._first_frame_event.set()
                self.on_playback_started(created_at=time.time())
            await self._audio_source.capture_frame(frame)
        logger.debug("_forward_audio: task ended (Chan closed)")

    def send_raw_message(self, message: str) -> None:
        self._ep.send_raw_message(self._sid, message)

    def on_attached(self) -> None:
        if self.next_in_chain:
            self.next_in_chain.on_attached()

    def on_detached(self) -> None:
        if self.next_in_chain:
            self.next_in_chain.on_detached()

    def __repr__(self) -> str:
        return f"AudioStreamOutput(label={self.label!r})"
