//! Audio resampler using speexdsp (same approach as FreeSWITCH).
//!
//! FreeSWITCH uses speex_resampler at quality 2 for telephony.
//! We default to quality 3 (SPEEX_RESAMPLER_QUALITY_VOIP) for slightly
//! better fidelity at negligible CPU cost for our concurrency levels.

use std::ptr;

// ─── speexdsp FFI bindings (minimal, matching FreeSWITCH usage) ──────────────

#[allow(non_camel_case_types)]
type spx_uint32_t = u32;
#[allow(non_camel_case_types)]
type spx_int16_t = i16;

#[repr(C)]
struct SpeexResamplerState {
    _opaque: [u8; 0],
}

extern "C" {
    fn speex_resampler_init(
        nb_channels: spx_uint32_t,
        in_rate: spx_uint32_t,
        out_rate: spx_uint32_t,
        quality: i32,
        err: *mut i32,
    ) -> *mut SpeexResamplerState;

    fn speex_resampler_process_interleaved_int(
        st: *mut SpeexResamplerState,
        input: *const spx_int16_t,
        in_len: *mut spx_uint32_t,
        output: *mut spx_int16_t,
        out_len: *mut spx_uint32_t,
    ) -> i32;

    fn speex_resampler_destroy(st: *mut SpeexResamplerState);
}

// ─── Quality constants (from speex_resampler.h) ──────────────────────────────

/// FreeSWITCH default — minimal latency, acceptable quality for telephony.
#[allow(dead_code)]
pub const QUALITY_FREESWITCH: i32 = 2;
/// Speex recommended for VoIP.
#[allow(dead_code)]
pub const QUALITY_VOIP: i32 = 3;
/// General purpose.
#[allow(dead_code)]
pub const QUALITY_DEFAULT: i32 = 4;
/// Desktop audio — our default. Good quality with ~10ms latency.
pub const QUALITY_DESKTOP: i32 = 5;

// ─── Resampler ───────────────────────────────────────────────────────────────

/// Audio resampler wrapping speexdsp, matching FreeSWITCH's resample implementation.
///
/// Lifecycle: create once, process many frames, drop to destroy.
/// The speex resampler maintains internal filter state across calls for
/// seamless frame-boundary continuity.
pub struct Resampler {
    state: *mut SpeexResamplerState,
    from_rate: u32,
    to_rate: u32,
    out_buf: Vec<i16>,
}

// SpeexResamplerState is thread-safe (no shared mutable state)
unsafe impl Send for Resampler {}

impl Resampler {
    /// Create a new resampler. Returns None if speexdsp init fails.
    ///
    /// `quality`: 0-10 (use QUALITY_VOIP for telephony).
    /// `channels`: number of audio channels (1 for mono).
    pub fn new(from_rate: u32, to_rate: u32, quality: i32, channels: u32) -> Option<Self> {
        if from_rate == to_rate {
            return None; // No resampling needed — caller should check is_needed()
        }

        let mut err: i32 = 0;
        let channels = if channels == 0 { 1 } else { channels };
        let state = unsafe {
            speex_resampler_init(channels, from_rate, to_rate, quality, &mut err)
        };

        if state.is_null() {
            return None;
        }

        // Pre-allocate output buffer for typical 20ms frame
        // Same sizing as FreeSWITCH: (to_rate / from_rate) * input_len
        let typical_input = (from_rate * 20 / 1000) as usize; // 20ms worth of samples
        let out_capacity = calc_output_size(to_rate, from_rate, typical_input);

        Some(Self {
            state,
            from_rate,
            to_rate,
            out_buf: vec![0i16; out_capacity],
        })
    }

    /// Create a mono resampler at desktop quality (speex quality 5).
    /// Good balance of quality and latency (~10ms) for voice AI.
    pub fn new_voip(from_rate: u32, to_rate: u32) -> Option<Self> {
        Self::new(from_rate, to_rate, QUALITY_DESKTOP, 1)
    }

    /// Check if resampling is needed between two rates.
    pub fn is_needed(from_rate: u32, to_rate: u32) -> bool {
        from_rate != to_rate
    }

    /// Resample a buffer of int16 PCM samples.
    ///
    /// Returns a slice of the resampled output. The returned slice is valid
    /// until the next call to `process()`.
    ///
    /// Sample counts are per-channel (for mono, same as total samples).
    pub fn process(&mut self, input: &[i16]) -> &[i16] {
        if input.is_empty() {
            return &[];
        }

        // Grow output buffer if needed (same as FreeSWITCH's dynamic realloc)
        let needed = calc_output_size(self.to_rate, self.from_rate, input.len());
        if needed > self.out_buf.len() {
            self.out_buf.resize(needed, 0);
        }

        let mut in_len = input.len() as u32;
        let mut out_len = self.out_buf.len() as u32;

        unsafe {
            speex_resampler_process_interleaved_int(
                self.state,
                input.as_ptr(),
                &mut in_len,
                self.out_buf.as_mut_ptr(),
                &mut out_len,
            );
        }

        &self.out_buf[..out_len as usize]
    }

    pub fn from_rate(&self) -> u32 { self.from_rate }
    pub fn to_rate(&self) -> u32 { self.to_rate }
}

impl Drop for Resampler {
    fn drop(&mut self) {
        if !self.state.is_null() {
            unsafe { speex_resampler_destroy(self.state); }
            self.state = ptr::null_mut();
        }
    }
}

/// Calculate output buffer size for given rates and input length.
/// Matches FreeSWITCH's `switch_resample_calc_buffer_size` macro.
fn calc_output_size(to_rate: u32, from_rate: u32, input_len: usize) -> usize {
    ((to_rate as f32 / from_rate as f32) * input_len as f32) as usize + 16
}

// ─── Tests ───────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_create_8k_to_16k() {
        let r = Resampler::new_voip(8000, 16000);
        assert!(r.is_some());
        let r = r.unwrap();
        assert_eq!(r.from_rate(), 8000);
        assert_eq!(r.to_rate(), 16000);
    }

    #[test]
    fn test_create_16k_to_8k() {
        let r = Resampler::new_voip(16000, 8000);
        assert!(r.is_some());
    }

    #[test]
    fn test_same_rate_returns_none() {
        assert!(Resampler::new_voip(16000, 16000).is_none());
    }

    #[test]
    fn test_is_needed() {
        assert!(Resampler::is_needed(8000, 16000));
        assert!(Resampler::is_needed(16000, 8000));
        assert!(!Resampler::is_needed(16000, 16000));
    }

    #[test]
    fn test_upsample_doubles_length() {
        let mut r = Resampler::new_voip(8000, 16000).unwrap();
        let input = vec![0i16; 160]; // 20ms at 8kHz
        let output = r.process(&input);
        assert_eq!(output.len(), 320); // 20ms at 16kHz
    }

    #[test]
    fn test_downsample_halves_length() {
        let mut r = Resampler::new_voip(16000, 8000).unwrap();
        let input = vec![0i16; 320]; // 20ms at 16kHz
        let output = r.process(&input);
        assert_eq!(output.len(), 160); // 20ms at 8kHz
    }

    #[test]
    fn test_empty_input() {
        let mut r = Resampler::new_voip(8000, 16000).unwrap();
        let output = r.process(&[]);
        assert!(output.is_empty());
    }

    #[test]
    fn test_sine_wave_preserved() {
        // Generate several frames of 200Hz sine at 16kHz to let the filter settle
        let frames = 5;
        let frame_samples = 320; // 20ms at 16kHz
        let total = frame_samples * frames;
        let input: Vec<i16> = (0..total)
            .map(|i| (((i as f64) * 200.0 * 2.0 * std::f64::consts::PI / 16000.0).sin() * 16000.0) as i16)
            .collect();

        // Downsample 16k→8k frame by frame (tests continuity)
        let mut down = Resampler::new_voip(16000, 8000).unwrap();
        let mut downsampled = Vec::new();
        for chunk in input.chunks(frame_samples) {
            downsampled.extend_from_slice(down.process(chunk));
        }

        // Upsample 8k→16k frame by frame
        let mut up = Resampler::new_voip(8000, 16000).unwrap();
        let mut roundtrip = Vec::new();
        for chunk in downsampled.chunks(160) {
            roundtrip.extend_from_slice(up.process(chunk));
        }

        // Check energy is preserved (skip first frame for filter warmup)
        let skip = frame_samples; // skip first 20ms
        let len = total.min(roundtrip.len()) - 2 * skip;
        let input_energy: f64 = input[skip..skip + len].iter().map(|&s| (s as f64).powi(2)).sum();
        let output_energy: f64 = roundtrip[skip..skip + len].iter().map(|&s| (s as f64).powi(2)).sum();
        let ratio = output_energy / input_energy;
        // Energy should be preserved within 3dB
        assert!(ratio > 0.5 && ratio < 2.0, "Energy ratio out of range: {:.3}", ratio);
    }

    #[test]
    fn test_multiple_frames_continuity() {
        // Process multiple frames — speex maintains state across calls
        let mut r = Resampler::new_voip(8000, 16000).unwrap();
        for _ in 0..10 {
            let input = vec![1000i16; 160]; // 20ms at 8kHz
            let output = r.process(&input);
            assert_eq!(output.len(), 320);
        }
    }

    #[test]
    fn test_quality_levels() {
        // All quality levels should work
        for q in 0..=10 {
            let r = Resampler::new(8000, 16000, q, 1);
            assert!(r.is_some(), "Quality {} failed", q);
        }
    }
}
