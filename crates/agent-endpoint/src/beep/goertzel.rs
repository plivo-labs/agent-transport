use std::f64::consts::PI;

/// Common voicemail beep frequencies (Hz).
///
/// 27 frequencies covering global voicemail/PBX standards:
/// - 400-500 Hz: US, UK, European, Canadian PSTN
/// - 550-850 Hz: PBX/VoIP systems
/// - 900-2000 Hz: Global standards, older equipment
pub const TARGET_FREQUENCIES: &[f64] = &[
    400.0, 425.0, 440.0, 450.0, 480.0, 500.0, // lower range
    550.0, 600.0, 620.0, 650.0, 700.0, 750.0, // mid-low
    800.0, 825.0, 850.0, // mid
    900.0, 950.0, 1000.0, 1100.0, 1200.0, // mid-high
    1300.0, 1400.0, 1500.0, 1600.0, 1700.0, // upper
    1800.0, 2000.0, // high
];

/// Indices of the 6 most common beep frequencies.
/// Checked first as a fast gate — if none of these show energy,
/// skip the full 27-frequency scan (~85% CPU savings on non-beep frames).
const FAST_GATE_INDICES: &[usize] = &[
    0,  // 400 Hz
    1,  // 425 Hz
    5,  // 500 Hz
    12, // 800 Hz
    13, // 825 Hz
    17, // 1000 Hz
];

/// Precomputed Goertzel coefficients for all target frequencies.
pub struct GoertzelFilter {
    sample_rate: u32,
    /// Goertzel coefficients: 2*cos(2*pi*f/sr) for each target frequency.
    coeffs: Vec<f64>,
    /// 2nd harmonic coefficients for harmonic rejection.
    harm2_coeffs: Vec<f64>,
    /// 3rd harmonic coefficients for harmonic rejection.
    harm3_coeffs: Vec<f64>,
    /// Lower spectral spread coefficients (freq - 100Hz).
    lower_coeffs: Vec<f64>,
    /// Upper spectral spread coefficients (freq + 100Hz).
    upper_coeffs: Vec<f64>,
}

impl GoertzelFilter {
    /// Create a new Goertzel filter with precomputed coefficients.
    pub fn new(sample_rate: u32) -> Self {
        let sr = sample_rate as f64;
        let coeff = |f: f64| 2.0 * (2.0 * PI * f / sr).cos();

        let coeffs: Vec<f64> = TARGET_FREQUENCIES.iter().map(|&f| coeff(f)).collect();
        let harm2_coeffs: Vec<f64> = TARGET_FREQUENCIES.iter().map(|&f| coeff(f * 2.0)).collect();
        let harm3_coeffs: Vec<f64> = TARGET_FREQUENCIES.iter().map(|&f| coeff(f * 3.0)).collect();
        let lower_coeffs: Vec<f64> = TARGET_FREQUENCIES
            .iter()
            .map(|&f| coeff((f - 100.0).max(50.0)))
            .collect();
        let upper_coeffs: Vec<f64> = TARGET_FREQUENCIES
            .iter()
            .map(|&f| coeff((f + 100.0).min(sr / 2.0 - 50.0)))
            .collect();

        Self {
            sample_rate,
            coeffs,
            harm2_coeffs,
            harm3_coeffs,
            lower_coeffs,
            upper_coeffs,
        }
    }

    /// Compute Goertzel magnitude for a single frequency coefficient.
    ///
    /// O(N) — processes each sample once. Much cheaper than FFT when
    /// you only need a few specific frequencies.
    fn magnitude(&self, samples: &[i16], coeff: f64) -> f64 {
        let mut s1: f64 = 0.0;
        let mut s2: f64 = 0.0;

        for &sample in samples {
            let s = sample as f64 + coeff * s1 - s2;
            s2 = s1;
            s1 = s;
        }

        let n = samples.len() as f64;
        let power = s1 * s1 + s2 * s2 - coeff * s1 * s2;
        power / (n * n)
    }

    /// Fast gate: check the 6 most common frequencies.
    /// Returns the max magnitude. If below threshold * 0.5, skip full scan.
    pub fn fast_gate(&self, samples: &[i16]) -> f64 {
        let mut max_mag = 0.0_f64;
        for &idx in FAST_GATE_INDICES {
            let mag = self.magnitude(samples, self.coeffs[idx]);
            max_mag = max_mag.max(mag);
        }
        max_mag
    }

    /// Full scan: check all 27 frequencies.
    /// Returns (best_index, best_magnitude).
    pub fn full_scan(&self, samples: &[i16]) -> (usize, f64) {
        let mut best_idx = 0;
        let mut best_mag = 0.0_f64;

        for (idx, &coeff) in self.coeffs.iter().enumerate() {
            let mag = self.magnitude(samples, coeff);
            if mag > best_mag {
                best_mag = mag;
                best_idx = idx;
            }
        }
        (best_idx, best_mag)
    }

    /// Check if the detected tone has significant harmonic content (= speech).
    ///
    /// Pure beeps are near-sinusoidal with negligible harmonics.
    /// Speech has strong 2nd/3rd harmonics from vocal fold vibration.
    ///
    /// Returns true if harmonics are too strong (likely speech, not a beep).
    pub fn has_harmonics(
        &self,
        samples: &[i16],
        freq_idx: usize,
        harmonic_threshold: f64,
        spectral_spread_threshold: f64,
    ) -> bool {
        let fund_mag = self.magnitude(samples, self.coeffs[freq_idx]);
        if fund_mag < 1e-10 {
            return true; // no fundamental = not a beep
        }

        // Step 1: Spectral spread (cheap — 2 Goertzel calls).
        // High energy in ±100Hz around fundamental = broadband = speech.
        let lower_ratio = self.magnitude(samples, self.lower_coeffs[freq_idx]) / fund_mag;
        let upper_ratio = self.magnitude(samples, self.upper_coeffs[freq_idx]) / fund_mag;
        if (lower_ratio + upper_ratio) > spectral_spread_threshold {
            return true;
        }

        // Step 2: Harmonic check (more expensive — 2 more Goertzel calls).
        // Only run if spectral spread passed.
        let harm2_ratio = self.magnitude(samples, self.harm2_coeffs[freq_idx]) / fund_mag;
        if harm2_ratio > harmonic_threshold {
            return true;
        }

        let harm3_ratio = self.magnitude(samples, self.harm3_coeffs[freq_idx]) / fund_mag;
        if harm3_ratio > harmonic_threshold {
            return true;
        }

        // Combined harmonics check (ITU-T standard).
        (harm2_ratio + harm3_ratio) > harmonic_threshold * 1.5
    }

    /// Get the frequency in Hz for a given index.
    pub fn frequency_hz(&self, idx: usize) -> f64 {
        TARGET_FREQUENCIES.get(idx).copied().unwrap_or(0.0)
    }

    /// Get the sample rate.
    pub fn sample_rate(&self) -> u32 {
        self.sample_rate
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Generate a pure sine wave at the given frequency.
    fn sine_wave(freq_hz: f64, sample_rate: u32, num_samples: usize) -> Vec<i16> {
        (0..num_samples)
            .map(|i| {
                let t = i as f64 / sample_rate as f64;
                (16000.0 * (2.0 * PI * freq_hz * t).sin()) as i16
            })
            .collect()
    }

    #[test]
    fn test_detects_1000hz_tone() {
        let filter = GoertzelFilter::new(16000);
        let samples = sine_wave(1000.0, 16000, 320); // 20ms at 16kHz

        let (best_idx, best_mag) = filter.full_scan(&samples);
        assert_eq!(filter.frequency_hz(best_idx), 1000.0);
        assert!(
            best_mag > 1.0,
            "magnitude should be above threshold: {best_mag}"
        );
    }

    #[test]
    fn test_detects_440hz_tone() {
        let filter = GoertzelFilter::new(16000);
        let samples = sine_wave(440.0, 16000, 320);

        let (best_idx, best_mag) = filter.full_scan(&samples);
        assert_eq!(filter.frequency_hz(best_idx), 440.0);
        assert!(best_mag > 1.0);
    }

    #[test]
    fn test_silence_below_threshold() {
        let filter = GoertzelFilter::new(16000);
        let samples = vec![0i16; 320]; // silence

        let (_, best_mag) = filter.full_scan(&samples);
        assert!(
            best_mag < 0.01,
            "silence should be below threshold: {best_mag}"
        );
    }

    #[test]
    fn test_fast_gate_detects_common_freq() {
        let filter = GoertzelFilter::new(16000);
        let samples = sine_wave(1000.0, 16000, 320);

        let max_mag = filter.fast_gate(&samples);
        assert!(max_mag > 1.0, "fast gate should detect 1000Hz: {max_mag}");
    }

    #[test]
    fn test_fast_gate_rejects_silence() {
        let filter = GoertzelFilter::new(16000);
        let samples = vec![0i16; 320];

        let max_mag = filter.fast_gate(&samples);
        assert!(max_mag < 0.01);
    }

    #[test]
    fn test_pure_tone_no_harmonics() {
        let filter = GoertzelFilter::new(16000);
        let samples = sine_wave(1000.0, 16000, 320);

        let (best_idx, _) = filter.full_scan(&samples);
        let has = filter.has_harmonics(&samples, best_idx, 0.10, 0.20);
        assert!(!has, "pure sine wave should not have significant harmonics");
    }

    #[test]
    fn test_complex_wave_has_harmonics() {
        let filter = GoertzelFilter::new(16000);
        // Fundamental + strong 2nd harmonic (simulates speech).
        // Harmonic at 50% amplitude → 25% power ratio, well above 10% threshold.
        let samples: Vec<i16> = (0..320)
            .map(|i| {
                let t = i as f64 / 16000.0;
                let fundamental = (2.0 * PI * 500.0 * t).sin();
                let harmonic2 = 0.5 * (2.0 * PI * 1000.0 * t).sin();
                ((fundamental + harmonic2) * 10000.0) as i16
            })
            .collect();

        let (best_idx, _) = filter.full_scan(&samples);
        let has = filter.has_harmonics(&samples, best_idx, 0.10, 0.20);
        assert!(has, "wave with strong 2nd harmonic should be rejected");
    }
}
