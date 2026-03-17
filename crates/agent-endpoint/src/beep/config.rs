/// Configuration for voicemail beep detection.
#[derive(Debug, Clone)]
pub struct BeepDetectorConfig {
    /// Audio sample rate in Hz.
    pub sample_rate: u32,
    /// Goertzel magnitude threshold for tone detection.
    /// Lower = more sensitive. Default: 1.0.
    pub threshold: f64,
    /// Minimum beep duration in milliseconds.
    /// Must be long enough to reject transient clicks but short enough
    /// to catch VoIP beeps (some are 40-60ms). Default: 50ms.
    pub min_duration_ms: u32,
    /// Maximum beep duration in milliseconds.
    /// Beeps longer than this are likely not beeps (e.g. hold music). Default: 5000ms.
    pub max_duration_ms: u32,
    /// Maximum time to wait for a beep in milliseconds.
    /// After this, give up and fire timeout. Default: 30000ms (30s).
    pub timeout_ms: u32,
    /// Harmonic rejection threshold (0.0 - 1.0).
    /// If 2nd/3rd harmonic energy exceeds this fraction of the fundamental,
    /// reject as speech. Default: 0.15 (relaxed from ITU-T 0.10 to handle
    /// codec artifacts in 8kHz PCMU telephony audio).
    pub harmonic_rejection: f64,
    /// Spectral spread threshold (0.0 - 1.0).
    /// Energy in ±100Hz around the fundamental — high spread = speech. Default: 0.25.
    pub spectral_spread_threshold: f64,
    /// Frequency drift tolerance in Hz.
    /// Allow the detected frequency to wobble without resetting the streak. Default: 25.0.
    pub freq_tolerance_hz: f64,
    /// Energy threshold below which a frame is considered silence.
    /// Used to skip processing on silent frames. Default: 5.0.
    pub silence_energy: f64,
    /// Number of consecutive silent frames required to confirm beep end.
    /// Higher = more robust for pulsed beeps but adds latency. Default: 3 (~60ms).
    pub post_beep_silence_frames: u32,
}

impl Default for BeepDetectorConfig {
    fn default() -> Self {
        Self::for_telephony()
    }
}

impl BeepDetectorConfig {
    /// Config optimized for telephony with 16kHz bridge rate.
    /// Relaxed harmonic rejection to handle codec quantization artifacts
    /// from PCMU/PCMA resampling.
    pub fn for_telephony() -> Self {
        Self {
            sample_rate: 16000,
            threshold: 1.0,
            min_duration_ms: 50,
            max_duration_ms: 5000,
            timeout_ms: 30_000,
            harmonic_rejection: 0.15,
            spectral_spread_threshold: 0.25,
            freq_tolerance_hz: 25.0,
            silence_energy: 5.0,
            post_beep_silence_frames: 3,
        }
    }

    /// Config for high-quality linear PCM audio (16kHz+).
    /// Stricter harmonic rejection since codec artifacts are minimal.
    pub fn for_linear_pcm(sample_rate: u32) -> Self {
        Self {
            sample_rate,
            threshold: 1.0,
            min_duration_ms: 50,
            max_duration_ms: 5000,
            timeout_ms: 30_000,
            harmonic_rejection: 0.10,
            spectral_spread_threshold: 0.20,
            freq_tolerance_hz: 25.0,
            silence_energy: 5.0,
            post_beep_silence_frames: 2,
        }
    }
}
