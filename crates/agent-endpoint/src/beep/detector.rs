use tracing::{debug, info};

use super::config::BeepDetectorConfig;
use super::goertzel::GoertzelFilter;

/// Result of processing one audio frame.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum BeepDetectorResult {
    /// Still listening — no beep detected yet.
    Listening,
    /// Beep detected and ended. Contains the event details.
    Detected(BeepEvent),
    /// Timed out waiting for a beep.
    Timeout,
}

/// Details of a detected beep.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct BeepEvent {
    /// Detected beep frequency in Hz (closest target frequency).
    pub frequency_hz: f64,
    /// Beep duration in milliseconds.
    pub duration_ms: u32,
    /// Peak Goertzel magnitude during the beep.
    pub magnitude: f64,
}

/// Voicemail beep detector.
///
/// Feed 20ms audio frames via `process_frame()`. When a beep is detected
/// (tone sustained for min_duration_ms, then silence), returns
/// `BeepDetectorResult::Detected`.
///
/// Algorithm:
/// 1. Energy check — skip silent frames
/// 2. Goertzel fast gate — check 6 common frequencies (400, 425, 500, 800, 825, 1000 Hz)
/// 3. If fast gate is promising, run full 27-frequency scan
/// 4. Harmonic rejection — filter out speech (pure beep = no harmonics)
/// 5. Streak tracking — sustained tone above threshold = beep
/// 6. Beep end — when tone stops and streak exceeded min_duration, fire event
pub struct BeepDetector {
    config: BeepDetectorConfig,
    filter: GoertzelFilter,
    /// Current detection streak in samples.
    streak_samples: u32,
    /// Tracked frequency index (must stay consistent within tolerance).
    tracked_freq_idx: Option<usize>,
    /// Peak magnitude during the current streak.
    peak_magnitude: f64,
    /// Total elapsed time in milliseconds since detection started.
    elapsed_ms: u32,
    /// Frame duration in milliseconds (derived from sample count and rate).
    frame_ms: u32,
    /// Whether we're in a beep (streak started).
    in_beep: bool,
    /// Consecutive silent frames after a beep (to confirm beep end).
    post_beep_silence_frames: u32,
}

impl BeepDetector {
    /// Create a new beep detector.
    pub fn new(config: BeepDetectorConfig) -> Self {
        let filter = GoertzelFilter::new(config.sample_rate);
        Self {
            config,
            filter,
            streak_samples: 0,
            tracked_freq_idx: None,
            peak_magnitude: 0.0,
            elapsed_ms: 0,
            frame_ms: 0,
            in_beep: false,
            post_beep_silence_frames: 0,
        }
    }

    /// Process one audio frame (typically 20ms of S16LE samples).
    ///
    /// Call this for every frame while waiting for a beep.
    /// Returns `Detected` when a beep is found, `Timeout` if max wait exceeded.
    pub fn process_frame(&mut self, samples: &[i16]) -> BeepDetectorResult {
        let frame_samples = samples.len() as u32;
        self.frame_ms = (frame_samples * 1000) / self.config.sample_rate;
        self.elapsed_ms += self.frame_ms;

        // Timeout check.
        if self.elapsed_ms >= self.config.timeout_ms {
            info!(elapsed_ms = self.elapsed_ms, "beep detection timeout");
            return BeepDetectorResult::Timeout;
        }

        // Energy check — skip silent frames.
        let energy = frame_energy(samples);
        if energy < self.config.silence_energy {
            return self.on_silence();
        }

        // Fast gate: check 6 common frequencies.
        let fast_mag = self.filter.fast_gate(samples);
        if fast_mag < self.config.threshold * 0.5 {
            return self.on_silence();
        }

        // Full scan: find the strongest frequency.
        let (best_idx, best_mag) = self.filter.full_scan(samples);
        if best_mag < self.config.threshold {
            return self.on_silence();
        }

        // Harmonic rejection — is this speech or a pure tone?
        if self.filter.has_harmonics(
            samples,
            best_idx,
            self.config.harmonic_rejection,
            self.config.spectral_spread_threshold,
        ) {
            debug!(
                freq_hz = self.filter.frequency_hz(best_idx),
                "rejected: harmonics detected (likely speech)"
            );
            return self.on_silence();
        }

        // Frequency consistency check.
        let detected_freq = self.filter.frequency_hz(best_idx);
        if let Some(tracked_idx) = self.tracked_freq_idx {
            let tracked_freq = self.filter.frequency_hz(tracked_idx);
            if (detected_freq - tracked_freq).abs() > self.config.freq_tolerance_hz {
                // Frequency jumped — reset streak, start tracking new frequency.
                debug!(
                    old_freq = tracked_freq,
                    new_freq = detected_freq,
                    "frequency drift — resetting streak"
                );
                self.streak_samples = 0;
                self.tracked_freq_idx = Some(best_idx);
                self.peak_magnitude = best_mag;
                self.in_beep = false;
                self.post_beep_silence_frames = 0;
            }
        } else {
            self.tracked_freq_idx = Some(best_idx);
        }

        // Extend the streak.
        self.streak_samples += frame_samples;
        self.peak_magnitude = self.peak_magnitude.max(best_mag);
        self.post_beep_silence_frames = 0;

        let streak_ms = (self.streak_samples * 1000) / self.config.sample_rate;
        if streak_ms >= self.config.min_duration_ms {
            self.in_beep = true;
        }

        // Max duration check — beep too long, likely not a beep.
        if streak_ms >= self.config.max_duration_ms {
            debug!(streak_ms, "streak exceeded max_duration — resetting");
            self.reset_streak();
            return BeepDetectorResult::Listening;
        }

        BeepDetectorResult::Listening
    }

    /// Handle a frame with no tone detected (silence or noise).
    fn on_silence(&mut self) -> BeepDetectorResult {
        if self.in_beep {
            // We were in a beep — count post-beep silence frames.
            self.post_beep_silence_frames += 1;

            // Require N consecutive silent frames to confirm the beep truly ended.
            // Default 3 frames (~60ms at 20ms/frame) for robustness against pulsed beeps.
            if self.post_beep_silence_frames >= self.config.post_beep_silence_frames {
                let streak_ms = (self.streak_samples * 1000) / self.config.sample_rate;
                let freq_hz = self
                    .tracked_freq_idx
                    .map(|idx| self.filter.frequency_hz(idx))
                    .unwrap_or(0.0);

                info!(
                    freq_hz,
                    duration_ms = streak_ms,
                    magnitude = self.peak_magnitude,
                    "beep detected"
                );

                let event = BeepEvent {
                    frequency_hz: freq_hz,
                    duration_ms: streak_ms,
                    magnitude: self.peak_magnitude,
                };
                self.reset_streak();
                return BeepDetectorResult::Detected(event);
            }
        } else {
            // Not in a beep — reset any partial streak.
            if self.streak_samples > 0 {
                self.reset_streak();
            }
        }
        BeepDetectorResult::Listening
    }

    /// Reset all streak state.
    fn reset_streak(&mut self) {
        self.streak_samples = 0;
        self.tracked_freq_idx = None;
        self.peak_magnitude = 0.0;
        self.in_beep = false;
        self.post_beep_silence_frames = 0;
    }

    /// Reset the detector for reuse (e.g. new call).
    pub fn reset(&mut self) {
        self.reset_streak();
        self.elapsed_ms = 0;
    }

    /// Elapsed time since detection started.
    pub fn elapsed_ms(&self) -> u32 {
        self.elapsed_ms
    }
}

/// Compute RMS energy of S16LE PCM samples (normalized).
fn frame_energy(samples: &[i16]) -> f64 {
    if samples.is_empty() {
        return 0.0;
    }
    let sum: f64 = samples.iter().map(|&s| (s as f64).abs()).sum();
    sum / samples.len() as f64
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::f64::consts::PI;

    fn sine_frame(freq_hz: f64, sample_rate: u32, num_samples: usize) -> Vec<i16> {
        (0..num_samples)
            .map(|i| {
                let t = i as f64 / sample_rate as f64;
                (16000.0 * (2.0 * PI * freq_hz * t).sin()) as i16
            })
            .collect()
    }

    fn silence_frame(num_samples: usize) -> Vec<i16> {
        vec![0i16; num_samples]
    }

    #[test]
    fn test_detects_beep_after_sustained_tone() {
        let config = BeepDetectorConfig {
            sample_rate: 16000,
            min_duration_ms: 80,
            ..Default::default()
        };
        let mut detector = BeepDetector::new(config);

        // Feed 100ms of 1000Hz tone (5 frames at 20ms each = 1600 samples)
        for _ in 0..5 {
            let frame = sine_frame(1000.0, 16000, 320);
            let result = detector.process_frame(&frame);
            assert_eq!(result, BeepDetectorResult::Listening);
        }

        // Feed silence frames to confirm beep end (need post_beep_silence_frames worth)
        let silence = silence_frame(320);
        let result1 = detector.process_frame(&silence);
        assert_eq!(result1, BeepDetectorResult::Listening);
        let result2 = detector.process_frame(&silence);
        assert_eq!(result2, BeepDetectorResult::Listening);
        let result3 = detector.process_frame(&silence);
        assert!(
            matches!(result3, BeepDetectorResult::Detected(e) if e.frequency_hz == 1000.0),
            "expected beep detection: {result3:?}"
        );
    }

    #[test]
    fn test_ignores_short_tone() {
        let config = BeepDetectorConfig {
            sample_rate: 16000,
            min_duration_ms: 80,
            ..Default::default()
        };
        let mut detector = BeepDetector::new(config);

        // Feed 20ms of tone (too short)
        let frame = sine_frame(1000.0, 16000, 320);
        detector.process_frame(&frame);

        // Then silence — should not trigger
        let silence = silence_frame(320);
        detector.process_frame(&silence);
        let result = detector.process_frame(&silence);
        assert_eq!(result, BeepDetectorResult::Listening);
    }

    #[test]
    fn test_timeout() {
        let config = BeepDetectorConfig {
            sample_rate: 16000,
            timeout_ms: 100, // 100ms timeout
            ..Default::default()
        };
        let mut detector = BeepDetector::new(config);

        // Feed silence until timeout
        let silence = silence_frame(320);
        for _ in 0..4 {
            detector.process_frame(&silence);
        }
        // 5th frame should be at 100ms → timeout
        let result = detector.process_frame(&silence);
        assert_eq!(result, BeepDetectorResult::Timeout);
    }

    #[test]
    fn test_ignores_silence() {
        let config = BeepDetectorConfig::default();
        let mut detector = BeepDetector::new(config);

        let silence = silence_frame(320);
        for _ in 0..10 {
            let result = detector.process_frame(&silence);
            assert_eq!(result, BeepDetectorResult::Listening);
        }
    }

    #[test]
    fn test_rejects_complex_wave() {
        let config = BeepDetectorConfig {
            sample_rate: 16000,
            min_duration_ms: 80,
            ..Default::default()
        };
        let mut detector = BeepDetector::new(config);

        // Wave with strong harmonics (simulates speech)
        let frame: Vec<i16> = (0..320)
            .map(|i| {
                let t = i as f64 / 16000.0;
                let f = (2.0 * PI * 500.0 * t).sin();
                let h2 = 0.3 * (2.0 * PI * 1000.0 * t).sin();
                let h3 = 0.15 * (2.0 * PI * 1500.0 * t).sin();
                ((f + h2 + h3) * 10000.0) as i16
            })
            .collect();

        // Feed 10 frames — should never detect
        for _ in 0..10 {
            let result = detector.process_frame(&frame);
            assert_eq!(result, BeepDetectorResult::Listening);
        }
    }

    #[test]
    fn test_reset() {
        let config = BeepDetectorConfig::default();
        let mut detector = BeepDetector::new(config);

        // Accumulate some state
        let frame = sine_frame(1000.0, 16000, 320);
        detector.process_frame(&frame);
        assert!(detector.elapsed_ms > 0);

        // Reset
        detector.reset();
        assert_eq!(detector.elapsed_ms(), 0);
        assert_eq!(detector.streak_samples, 0);
    }
}
