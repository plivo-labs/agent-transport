"""Rust-backed audio mixers for Pipecat.

Drop-in replacements for pipecat.audio.mixers that route background audio
through Rust's 20ms send loop instead of mixing in Python.

Usage:
    from agent_transport.audio_stream.pipecat.mixers import SoundfileMixer

    mixer = SoundfileMixer(
        transport=transport,
        sound_files={"hold": "hold_music.wav"},
        default_sound="hold",
    )
    params = TransportParams(audio_out_mixer=mixer)
"""

import asyncio
from typing import Any, Dict, Mapping, Optional

import numpy as np
from loguru import logger

try:
    from pipecat.audio.mixers.base_audio_mixer import BaseAudioMixer
    from pipecat.frames.frames import MixerControlFrame, MixerEnableFrame, MixerUpdateSettingsFrame
except ImportError:
    raise ImportError("pipecat-ai is required: pip install pipecat-ai")

try:
    import soundfile as sf
except ImportError:
    sf = None


class SoundfileMixer(BaseAudioMixer):
    """Rust-backed soundfile mixer.

    Drop-in replacement for pipecat.audio.mixers.SoundfileMixer.
    Instead of mixing audio in Python (numpy), this feeds background audio
    to Rust's send loop via send_background_audio(). Rust mixes with agent
    voice at 20ms intervals — zero GIL overhead on the hot path.

    mix() returns agent audio unchanged (passthrough). Actual mixing
    happens in Rust at the wire level.
    """

    def __init__(
        self,
        transport: "AudioStreamTransport",
        *,
        sound_files: Mapping[str, str],
        default_sound: str,
        volume: float = 0.4,
        mixing: bool = True,
        loop: bool = True,
    ):
        if sf is None:
            raise ImportError("soundfile is required: pip install soundfile")
        self._transport = transport
        self._sound_files = sound_files
        self._default_sound = default_sound
        self._volume = volume
        self._mixing = mixing
        self._loop = loop
        self._sounds: Dict[str, np.ndarray] = {}
        self._current_sound = default_sound
        self._sample_rate = 0
        self._feed_task: Optional[asyncio.Task] = None
        self._sound_pos = 0
        self._lock = asyncio.Lock()

    async def start(self, sample_rate: int):
        self._sample_rate = sample_rate
        for name, path in self._sound_files.items():
            await asyncio.to_thread(self._load_sound_file, name, path)
        self._feed_task = asyncio.create_task(self._feed_loop())

    async def stop(self):
        self._mixing = False
        if self._feed_task:
            self._feed_task.cancel()
            try:
                await self._feed_task
            except (asyncio.CancelledError, Exception):
                pass

    async def process_frame(self, frame: MixerControlFrame):
        if isinstance(frame, MixerUpdateSettingsFrame):
            for setting, value in frame.settings.items():
                if setting == "sound" and value in self._sounds:
                    self._current_sound = value
                    async with self._lock:
                        self._sound_pos = 0
                elif setting == "volume":
                    self._volume = float(value)
                elif setting == "loop":
                    self._loop = bool(value)
        elif isinstance(frame, MixerEnableFrame):
            self._mixing = frame.enable

    async def mix(self, audio: bytes) -> bytes:
        """Passthrough — Rust mixes background audio at the wire level."""
        return audio

    async def _feed_loop(self):
        """Feed background audio chunks to Rust's send_background_audio at 20ms."""
        chunk_samples = self._sample_rate // 50  # 20ms
        while True:
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                break

            async with self._lock:
                if not self._mixing:
                    continue

                sound = self._sounds.get(self._current_sound)
                if sound is None or len(sound) == 0:
                    continue

                if self._sound_pos + chunk_samples > len(sound):
                    if self._loop:
                        self._sound_pos = 0
                    else:
                        continue

                chunk = sound[self._sound_pos:self._sound_pos + chunk_samples]
                self._sound_pos += chunk_samples

            # Apply volume and convert to bytes
            scaled = np.clip(chunk * self._volume, -32768, 32767).astype(np.int16)
            try:
                self._transport.send_background_audio(
                    scaled.tobytes(), self._sample_rate, 1,
                )
            except Exception as e:
                logger.warning("Background audio feed stopped: %s", e)
                break  # Session gone

    def _load_sound_file(self, name: str, path: str):
        try:
            sound, sr = sf.read(path, dtype="int16")
            if sr != self._sample_rate:
                logger.info("Resampling sound %s from %dHz to %dHz", path, sr, self._sample_rate)
                sound = np.interp(
                    np.linspace(0, len(sound), int(len(sound) * self._sample_rate / sr)),
                    np.arange(len(sound)),
                    sound,
                ).astype(np.int16)
            self._sounds[name] = np.asarray(sound, dtype=np.int16)
            logger.debug("Loaded mixer sound %s from %s", name, path)
        except Exception as e:
            logger.error("Failed to load sound %s: %s", path, e)
