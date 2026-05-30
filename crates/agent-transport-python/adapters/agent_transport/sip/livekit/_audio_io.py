"""TransportAudioInput / TransportAudioOutput — single classes for both
SIP/RTP and Plivo audio_stream transports.

* :class:`TransportAudioInput` is a thin pure-Python ``AudioInput``
  subclass that forwards Rust-received frames into a ``Chan`` (one per
  LiveKit pipeline). No Pattern A inheritance because LiveKit's
  ``_ParticipantAudioInputStream`` is tightly coupled to ``rtc.Room``'s
  remote-track subscription model (``_on_track_available``,
  ``_create_stream`` etc.) — replicating that surface around a Rust
  endpoint produces more glue than parallel code.

* :class:`TransportAudioOutput` subclasses LiveKit's
  ``_ParticipantAudioOutput`` (Pattern A inheritance). The base supplies
  ``_forward_audio``, ``_wait_for_playout``, ``capture_frame``,
  ``flush``, ``clear_buffer`` — all of which we want LiveKit-verbatim.
  Three things we override:

    1. ``__init__`` swaps the parent's ``rtc.AudioSource`` for our
       :class:`TransportAudioSource` (Rust-backed).
    2. ``_publish_track`` becomes a no-op that resolves
       ``_subscribed_fut`` immediately — there's no WebRTC track to
       publish; audio flows directly to the Rust endpoint.
    3. ``pause`` / ``resume`` additionally call
       ``ep.pause``/``ep.resume`` so the Rust send loop is suspended at
       the transport layer (saves cycles + matters for some providers
       that bill on packets out, like Plivo).

  An orphan ``rtc.AudioSource`` is allocated inside the parent's
  ``__init__`` and immediately replaced. The orphan's FFI handle is
  reclaimed by Python GC when nothing references it. One wasted
  allocation per call setup — acceptable cost for Pattern A inheritance
  versus the brittleness of replicating the parent's body.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from livekit import rtc
from livekit.agents.voice.io import AudioInput
from livekit.agents.voice.room_io._output import _ParticipantAudioOutput

from agent_transport._executors import audio_io_executor

from ._audio_source import TransportAudioSource
from ._aio_utils import cancel_and_wait
from ._channel import Chan
from ._fake_room import _FakeRoom

logger = logging.getLogger(__name__)


def _to_livekit_frame(audio_bytes: bytes, sample_rate: int, num_channels: int) -> rtc.AudioFrame:
    samples_per_channel = len(audio_bytes) // (2 * num_channels)
    return rtc.AudioFrame(
        data=audio_bytes, sample_rate=sample_rate,
        num_channels=num_channels, samples_per_channel=samples_per_channel,
    )


# ─── AudioInput ──────────────────────────────────────────────────────────────


class TransportAudioInput(AudioInput):
    """Async iterator yielding ``rtc.AudioFrame`` from a SIP or audio_stream call.

    Architecture mirrors LiveKit's ``_ParticipantAudioInputStream``:

    * A forwarding task reads from Rust (``recv_audio_bytes_blocking``
      via ``run_in_executor``) and pushes into a ``Chan``.
    * ``__anext__`` reads from the Chan.
    * On stream end, pushes 0.5s of silence to flush downstream STT
      buffers before closing — matches LiveKit exactly.
    """

    def __init__(
        self,
        endpoint,
        session_id: str,
        *,
        label: str = "transport-audio-input",
        source=None,
        **kwargs,
    ) -> None:
        try:
            super().__init__(label=label, source=source)
        except TypeError:
            # Older livekit-agents versions take no source kwarg.
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
    def label(self) -> str:
        return self._label

    @property
    def source(self):
        return self._source

    async def start(self) -> None:
        if self._forward_task is None:
            self._forward_task = asyncio.create_task(self._forward_audio())

    async def __anext__(self) -> rtc.AudioFrame:
        if self._source:
            return await self._source.__anext__()
        if self._forward_task is None:
            await self.start()
        return await self._data_ch.__anext__()

    def __aiter__(self):
        return self

    async def _forward_audio(self) -> None:
        loop = asyncio.get_running_loop()
        frame_count = 0
        try:
            while not self._closed:
                try:
                    result = await loop.run_in_executor(
                        audio_io_executor(),
                        lambda: self._ep.recv_audio_bytes_blocking(self._sid, 20) if not self._closed else None,
                    )
                except Exception as e:
                    logger.debug("TransportAudioInput recv error: %s", e)
                    break
                if result is not None and self._attached:
                    ab, sr, nc = result
                    frame = _to_livekit_frame(bytes(ab), sr, nc)
                    try:
                        await self._data_ch.send(frame)
                    except Exception:
                        # Chan closed mid-loop (aclose / teardown).
                        logger.debug("TransportAudioInput: data_ch send failed, exiting")
                        break
                    frame_count += 1
                    if frame_count == 1:
                        logger.info(
                            "TransportAudioInput: first frame received sr=%d samples=%d",
                            sr, frame.samples_per_channel,
                        )
                    elif frame_count % 250 == 0:  # every 5 seconds
                        logger.info(
                            "TransportAudioInput: %d frames forwarded (%.1fs)",
                            frame_count, frame_count * 0.02,
                        )
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
        if self._source:
            self._source.on_attached()

    def on_detached(self) -> None:
        self._attached = False
        if self._source:
            self._source.on_detached()

    async def aclose(self) -> None:
        self._closed = True
        if self._forward_task:
            await cancel_and_wait(self._forward_task)
        self._data_ch.close()

    def __repr__(self) -> str:
        return f"TransportAudioInput(label={self._label!r}, source={self._source!r})"


# ─── AudioOutput — Pattern A inheritance ─────────────────────────────────────


class TransportAudioOutput(_ParticipantAudioOutput):
    """Pattern A: subclass LiveKit's ``_ParticipantAudioOutput`` so the
    buffering / forward / interrupt / playout logic is LiveKit-verbatim.

    We replace the parent's ``rtc.AudioSource`` with a Rust-backed
    :class:`TransportAudioSource` and short-circuit ``_publish_track``
    (there's no WebRTC publish step — audio goes straight to the Rust
    endpoint).
    """

    def __init__(
        self,
        endpoint,
        session_id: str,
        *,
        sample_rate: Optional[int] = None,
        num_channels: int = 1,
        events=None,
    ) -> None:
        _sample_rate = sample_rate or endpoint.output_sample_rate

        # Construct the parent with a fake room + a default
        # TrackPublishOptions. The parent allocates an orphan
        # rtc.AudioSource which we immediately replace below; its FFI
        # handle is reclaimed by Python GC when this scope ends.
        super().__init__(
            room=_FakeRoom(identity="agent"),
            sample_rate=_sample_rate,
            num_channels=num_channels,
            track_publish_options=rtc.TrackPublishOptions(),
        )

        # Swap the WebRTC source for our Rust-backed adapter. Same surface
        # (capture_frame, wait_for_playout, queued_duration, clear_queue,
        # aclose) so the parent's _forward_audio / _wait_for_playout
        # don't notice. ``events`` defaults to the process-global
        # FfiQueue inside TransportAudioSource.
        self._audio_source = TransportAudioSource(
            endpoint, session_id,
            sample_rate=_sample_rate,
            num_channels=num_channels,
            queue_size_ms=200,  # matches LiveKit _ParticipantAudioOutput
            events=events,
        )

        # Transport-specific state for pause/resume / send_raw_message.
        self._ep = endpoint
        self._sid = session_id
        self._rust_paused = False

    async def _publish_track(self) -> None:
        """No-op override: there's no WebRTC track to publish.

        ``_ParticipantAudioOutput.start`` awaits ``self._subscribed_fut``
        before allowing ``capture_frame`` to flow — resolving it here
        unblocks the pipeline immediately. ``self._lock`` mirrors the
        parent's lock semantics (LiveKit uses it to serialize republish
        on reconnection).
        """
        async with self._lock:
            if not self._subscribed_fut.done():
                self._subscribed_fut.set_result(None)

    async def capture_frame(self, frame) -> None:
        """Auto-start ``_forwarding_task`` before delegating to parent.

        LiveKit's ``_ParticipantAudioOutput`` doesn't auto-start its
        forwarding task — ``RoomIO.start`` is the canonical caller (see
        ``livekit/agents/voice/room_io/room_io.py:371``). We bypass
        ``RoomIO`` entirely (audio flows through our Rust transport, not
        through ``rtc.Room.local_participant``), so without this guard
        ``_forward_audio`` never runs, frames just pile into
        ``_audio_buf``, and the agent never produces output. Mirrors the
        ``if self._forwarding_task is None: await self.start()`` guard
        the pre-Tier-B ``SipAudioOutput`` had.
        """
        if self._forwarding_task is None:
            logger.debug(
                "TransportAudioOutput.capture_frame: auto-starting forwarding task (sid=%s)",
                self._sid,
            )
            await self.start()
        await super().capture_frame(frame)

    def pause(self) -> None:
        """Pause the playout pipeline AND signal Rust to suspend its
        send loop. Plivo (and several SIP providers) bill per-packet so
        muting at the transport layer is cheaper than draining a stream
        of silence frames.
        """
        super().pause()
        if not self._rust_paused:
            try:
                self._ep.pause(self._sid)
                self._rust_paused = True
            except Exception:
                logger.warning(
                    "TransportAudioOutput.pause failed for session %s",
                    self._sid, exc_info=True,
                )

    def resume(self) -> None:
        super().resume()
        if self._rust_paused:
            try:
                self._ep.resume(self._sid)
                self._rust_paused = False
            except Exception:
                logger.warning(
                    "TransportAudioOutput.resume failed for session %s",
                    self._sid, exc_info=True,
                )

    def send_raw_message(self, message: str) -> None:
        """Plivo-audio_stream-only extension: send a raw JSON message
        upstream over the WebSocket. Used by ``TransportRoom`` to
        forward Plivo control messages (e.g., ``playAudio``) that don't
        fit the audio-frame channel.

        No-op on SIP endpoints (which raise inside ``ep.send_raw_message``
        — we let that surface as a logged warning rather than silently
        succeeding, since calling this on SIP is always a bug).
        """
        try:
            self._ep.send_raw_message(self._sid, message)
        except Exception:
            logger.warning(
                "TransportAudioOutput.send_raw_message failed for session %s",
                self._sid, exc_info=True,
            )

    def __repr__(self) -> str:
        return f"TransportAudioOutput(label={self.label!r}, session={self._sid!r})"
