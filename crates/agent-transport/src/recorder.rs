//! Stereo WAV file recorder for call audio.
//!
//! Records both directions of a call into a single stereo WAV file:
//! - Left channel: user (inbound RTP, decoded + resampled to 16kHz)
//! - Right channel: agent (outbound AudioBuffer, 16kHz)
//!
//! Write-through design: samples are interleaved and written immediately,
//! no accumulation buffers. Zero CPU encoding overhead (raw PCM).

use std::collections::VecDeque;
use std::fs::File;
use std::io::{Seek, SeekFrom, Write};
use std::sync::Mutex;

use tracing::info;

const SAMPLE_RATE: u32 = 16000;
const BITS_PER_SAMPLE: u16 = 16;

/// Recording mode: stereo (L=user, R=agent) or mono (mixed).
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum RecordingMode {
    /// Stereo: left channel = user, right channel = agent
    Stereo,
    /// Mono: user + agent mixed into single channel
    Mono,
}

/// WAV recorder shared between RTP send and recv loops.
/// Thread-safe via internal mutex.
pub(crate) struct CallRecorder {
    inner: Mutex<RecorderInner>,
}

struct RecorderInner {
    file: File,
    mode: RecordingMode,
    /// Pending user samples not yet paired with agent samples
    user_pending: VecDeque<i16>,
    /// Pending agent samples not yet paired with user samples
    agent_pending: VecDeque<i16>,
    /// Total frames written
    frames_written: u32,
}

impl CallRecorder {
    pub fn new(path: &str, mode: RecordingMode) -> std::io::Result<Self> {
        let mut f = File::create(path)?;
        // Write placeholder WAV header (44 bytes), finalized on close
        f.write_all(&[0u8; 44])?;
        info!("Recording to {} ({:?})", path, mode);
        Ok(Self {
            inner: Mutex::new(RecorderInner {
                file: f,
                mode,
                user_pending: VecDeque::new(),
                agent_pending: VecDeque::new(),
                frames_written: 0,
            }),
        })
    }

    /// Called from RTP recv loop after G.711 decode + 8→16kHz resample.
    pub fn write_user_samples(&self, samples: &[i16]) {
        let mut inner = self.inner.lock().unwrap();
        inner.user_pending.extend(samples.iter().copied());
        Self::flush_paired(&mut inner);
    }

    /// Called from RTP send loop — the 16kHz samples about to be downsampled and sent.
    pub fn write_agent_samples(&self, samples: &[i16]) {
        let mut inner = self.inner.lock().unwrap();
        inner.agent_pending.extend(samples.iter().copied());
        Self::flush_paired(&mut inner);
    }

    /// Flush paired samples to file — stereo interleaved or mono mixed.
    fn flush_paired(inner: &mut RecorderInner) {
        let n = inner.user_pending.len().min(inner.agent_pending.len());
        if n == 0 {
            return;
        }

        match inner.mode {
            RecordingMode::Stereo => {
                // Interleaved stereo: [L0, R0, L1, R1, ...]
                for _ in 0..n {
                    let user = inner.user_pending.pop_front().unwrap();
                    let agent = inner.agent_pending.pop_front().unwrap();
                    let _ = inner.file.write_all(&user.to_le_bytes());
                    let _ = inner.file.write_all(&agent.to_le_bytes());
                }
            }
            RecordingMode::Mono => {
                // Mix: (user + agent) / 2, clamped to i16 range
                for _ in 0..n {
                    let user = inner.user_pending.pop_front().unwrap() as i32;
                    let agent = inner.agent_pending.pop_front().unwrap() as i32;
                    let mixed = ((user + agent) / 2).clamp(i16::MIN as i32, i16::MAX as i32) as i16;
                    let _ = inner.file.write_all(&mixed.to_le_bytes());
                }
            }
        }
        inner.frames_written += n as u32;
    }

    /// Flush any remaining unpaired samples (pad the shorter channel with silence)
    /// and write the WAV header.
    pub fn finalize(&self) {
        let mut inner = self.inner.lock().unwrap();

        // Flush remaining unpaired samples — pad shorter channel with silence
        while !inner.user_pending.is_empty() || !inner.agent_pending.is_empty() {
            let user = inner.user_pending.pop_front().unwrap_or(0);
            let agent = inner.agent_pending.pop_front().unwrap_or(0);
            match inner.mode {
                RecordingMode::Stereo => {
                    let _ = inner.file.write_all(&user.to_le_bytes());
                    let _ = inner.file.write_all(&agent.to_le_bytes());
                }
                RecordingMode::Mono => {
                    let mixed = (((user as i32) + (agent as i32)) / 2)
                        .clamp(i16::MIN as i32, i16::MAX as i32) as i16;
                    let _ = inner.file.write_all(&mixed.to_le_bytes());
                }
            }
            inner.frames_written += 1;
        }

        // Write WAV header
        let num_channels: u16 = match inner.mode {
            RecordingMode::Stereo => 2,
            RecordingMode::Mono => 1,
        };
        let data_size = inner.frames_written * (num_channels as u32) * (BITS_PER_SAMPLE as u32 / 8);
        let byte_rate = SAMPLE_RATE * (num_channels as u32) * (BITS_PER_SAMPLE as u32 / 8);
        let block_align = num_channels * (BITS_PER_SAMPLE / 8);

        let _ = inner.file.seek(SeekFrom::Start(0));
        let _ = inner.file.write_all(b"RIFF");
        let _ = inner.file.write_all(&(36 + data_size).to_le_bytes());
        let _ = inner.file.write_all(b"WAVEfmt ");
        let _ = inner.file.write_all(&16u32.to_le_bytes());
        let _ = inner.file.write_all(&1u16.to_le_bytes()); // PCM
        let _ = inner.file.write_all(&num_channels.to_le_bytes());
        let _ = inner.file.write_all(&SAMPLE_RATE.to_le_bytes());
        let _ = inner.file.write_all(&byte_rate.to_le_bytes());
        let _ = inner.file.write_all(&block_align.to_le_bytes());
        let _ = inner.file.write_all(&BITS_PER_SAMPLE.to_le_bytes());
        let _ = inner.file.write_all(b"data");
        let _ = inner.file.write_all(&data_size.to_le_bytes());

        let duration_s = inner.frames_written as f64 / SAMPLE_RATE as f64;
        info!("Recording finalized: {:.1}s, {:?}, {} frames, {:.1} MB",
            duration_s, inner.mode, inner.frames_written, data_size as f64 / 1_048_576.0);
    }
}
