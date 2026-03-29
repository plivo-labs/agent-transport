/// A PCM audio frame, compatible with LiveKit's AudioFrame format.
///
/// Samples are signed 16-bit integers, interleaved by channel.
/// For 20ms of PCMU/PCMA at 8kHz mono: 160 samples.
#[derive(Debug, Clone)]
pub struct AudioFrame {
    /// PCM samples, interleaved by channel.
    pub data: Vec<i16>,

    /// Sample rate in Hz (matches the pipeline sample rate configured on the endpoint).
    pub sample_rate: u32,

    /// Number of audio channels (1 = mono, 2 = stereo).
    pub num_channels: u32,

    /// Number of samples per channel in this frame.
    pub samples_per_channel: u32,
}

impl AudioFrame {
    /// Create a new audio frame.
    pub fn new(data: Vec<i16>, sample_rate: u32, num_channels: u32) -> Self {
        let samples_per_channel = if num_channels > 0 {
            data.len() as u32 / num_channels
        } else {
            0
        };
        Self {
            data,
            sample_rate,
            num_channels,
            samples_per_channel,
        }
    }

    /// Create a silent frame with the given parameters.
    pub fn silence(sample_rate: u32, num_channels: u32, duration_ms: u32) -> Self {
        let samples_per_channel = sample_rate * duration_ms / 1000;
        let total_samples = (samples_per_channel * num_channels) as usize;
        Self {
            data: vec![0i16; total_samples],
            sample_rate,
            num_channels,
            samples_per_channel,
        }
    }

    /// Duration of this frame in milliseconds.
    pub fn duration_ms(&self) -> u32 {
        if self.sample_rate == 0 {
            return 0;
        }
        self.samples_per_channel * 1000 / self.sample_rate
    }

    /// Total number of samples (across all channels).
    pub fn total_samples(&self) -> usize {
        self.data.len()
    }

    /// Get the raw bytes (little-endian i16).
    pub fn as_bytes(&self) -> Vec<u8> {
        self.data
            .iter()
            .flat_map(|s| s.to_le_bytes())
            .collect()
    }

    /// Create from raw bytes (little-endian i16).
    pub fn from_bytes(bytes: &[u8], sample_rate: u32, num_channels: u32) -> Self {
        let data: Vec<i16> = bytes
            .chunks_exact(2)
            .map(|chunk| i16::from_le_bytes([chunk[0], chunk[1]]))
            .collect();
        Self::new(data, sample_rate, num_channels)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_silence_frame() {
        let frame = AudioFrame::silence(48000, 1, 20);
        assert_eq!(frame.samples_per_channel, 960);
        assert_eq!(frame.total_samples(), 960);
        assert_eq!(frame.duration_ms(), 20);
    }

    #[test]
    fn test_bytes_roundtrip() {
        let original = AudioFrame::new(vec![100, -200, 300, -400], 8000, 1);
        let bytes = original.as_bytes();
        let restored = AudioFrame::from_bytes(&bytes, 8000, 1);
        assert_eq!(original.data, restored.data);
    }
}
