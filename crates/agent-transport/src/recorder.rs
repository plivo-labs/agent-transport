//! Call recording — stereo OGG/Opus with batch encoding.
//!
//! Architecture:
//! - Send/recv loops (SIP RTP or audio stream WS) push samples into per-call ring buffers
//! - Single shared encoder thread wakes every 2.5s, drains all ring buffers
//! - Interleaves to stereo (L=user, R=agent), Opus-encodes, writes OGG packets
//! - Records at native sample rate — no resampling
//! - Works for both SIP and audio streaming transports
//!
//! CPU: ~0.05% per call. 500 calls = ~25% of one core.

use std::borrow::Cow;
use std::collections::HashMap;
use std::collections::VecDeque;
use std::fs::File;
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use opus::Encoder as OpusEncoder;
use opus::{Application, Channels};
use ogg::writing::PacketWriter;

use tracing::{error, info, warn};

const BATCH_INTERVAL_MS: u64 = 2500;

/// Recording mode.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum RecordingMode {
    Stereo,
    Mono,
}

// ─── CallRecorder (per-call ring buffers) ────────────────────────────────────

/// Per-call recording state. Send/recv loops push samples here.
/// No file handle, no encoding — just accumulation.
pub(crate) struct CallRecorder {
    inner: Mutex<RecorderInner>,
}

struct RecorderInner {
    user_pending: VecDeque<i16>,
    agent_pending: VecDeque<i16>,
    sample_rate: u32,
    mode: RecordingMode,
    finalized: bool,
}

impl CallRecorder {
    fn new(sample_rate: u32, mode: RecordingMode) -> Self {
        Self {
            inner: Mutex::new(RecorderInner {
                user_pending: VecDeque::with_capacity(sample_rate as usize * 3),
                agent_pending: VecDeque::with_capacity(sample_rate as usize * 3),
                sample_rate,
                mode,
                finalized: false,
            }),
        }
    }

    pub fn write_user_samples(&self, samples: &[i16]) {
        if let Ok(mut inner) = self.inner.lock() {
            if !inner.finalized {
                inner.user_pending.extend(samples.iter().copied());
            }
        }
    }

    pub fn write_agent_samples(&self, samples: &[i16]) {
        if let Ok(mut inner) = self.inner.lock() {
            if !inner.finalized {
                inner.agent_pending.extend(samples.iter().copied());
            }
        }
    }

    fn drain(&self) -> (Vec<i16>, Vec<i16>, u32, RecordingMode) {
        let mut inner = self.inner.lock().unwrap();
        let user: Vec<i16> = inner.user_pending.drain(..).collect();
        let agent: Vec<i16> = inner.agent_pending.drain(..).collect();
        (user, agent, inner.sample_rate, inner.mode)
    }

    fn is_finalized(&self) -> bool {
        self.inner.lock().unwrap().finalized
    }

    fn mark_finalized(&self) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.finalized = true;
        }
    }
}

// ─── OGG/Opus encoder state ─────────────────────────────────────────────────

struct OggState {
    /// Persistent PacketWriter — keeps page sequence numbers correct across batches.
    /// Using BufWriter<File> as the owned writer so PacketWriter can live in the struct.
    writer: PacketWriter<'static, std::io::BufWriter<File>>,
    serial: u32,
    encoder: OpusEncoder,
    granule_pos: u64,
    sample_rate: u32,
    num_channels: u16,
    header_written: bool,
}

impl OggState {
    fn new(path: &str, sample_rate: u32, mode: RecordingMode) -> Result<Self, String> {
        let file = File::create(path).map_err(|e| format!("create {}: {}", path, e))?;
        let num_channels: u16 = if mode == RecordingMode::Stereo { 2 } else { 1 };
        let channels = if num_channels == 2 { Channels::Stereo } else { Channels::Mono };

        let opus_sr = match sample_rate {
            r if r <= 8000 => 8000,
            r if r <= 12000 => 12000,
            r if r <= 16000 => 16000,
            r if r <= 24000 => 24000,
            _ => 48000,
        };

        let encoder = OpusEncoder::new(opus_sr, channels, Application::Voip)
            .map_err(|e| format!("opus: {}", e))?;

        let serial = rand::random::<u32>();
        let writer = PacketWriter::new(std::io::BufWriter::new(file));

        Ok(Self {
            writer,
            serial,
            encoder,
            granule_pos: 0,
            sample_rate: opus_sr,
            num_channels,
            header_written: false,
        })
    }

    fn write_headers(&mut self) -> Result<(), String> {
        if self.header_written { return Ok(()); }

        // OpusHead — RFC 7845 §5.1
        let lookahead = self.encoder.get_lookahead().unwrap_or(312) as u64;
        let pre_skip: u16 = (lookahead * 48000 / self.sample_rate as u64) as u16;
        let mut id = Vec::with_capacity(19);
        id.extend_from_slice(b"OpusHead");
        id.push(1); // version
        id.push(self.num_channels as u8);
        id.extend_from_slice(&pre_skip.to_le_bytes());
        id.extend_from_slice(&self.sample_rate.to_le_bytes()); // input sample rate (informational)
        id.extend_from_slice(&0i16.to_le_bytes()); // output gain
        id.push(0); // channel mapping family

        // OpusTags — RFC 7845 §5.2
        let vendor = b"agent-transport";
        let mut comment = Vec::with_capacity(20 + vendor.len());
        comment.extend_from_slice(b"OpusTags");
        comment.extend_from_slice(&(vendor.len() as u32).to_le_bytes());
        comment.extend_from_slice(vendor);
        comment.extend_from_slice(&0u32.to_le_bytes());

        self.writer.write_packet(Cow::Owned(id), self.serial, ogg::writing::PacketWriteEndInfo::EndPage, 0)
            .map_err(|e| format!("ogg: {}", e))?;
        self.writer.write_packet(Cow::Owned(comment), self.serial, ogg::writing::PacketWriteEndInfo::EndPage, 0)
            .map_err(|e| format!("ogg: {}", e))?;

        self.header_written = true;
        Ok(())
    }

    fn encode_batch(&mut self, interleaved: &[i16]) -> Result<(), String> {
        if interleaved.is_empty() { return Ok(()); }
        if !self.header_written { self.write_headers()?; }

        // Opus frame: 20ms at encoder sample rate, interleaved channels
        let frame_samples = (self.sample_rate as usize / 50) * self.num_channels as usize;
        // Granule position is ALWAYS at 48kHz per RFC 7845, regardless of encoder rate
        let granule_increment: u64 = 48000 / 50; // 960 per 20ms frame
        let mut out_buf = vec![0u8; 4000];
        let chunks: Vec<_> = interleaved.chunks(frame_samples).collect();
        let last_idx = chunks.len().saturating_sub(1);

        for (idx, chunk) in chunks.iter().enumerate() {
            let samples = if chunk.len() < frame_samples {
                let mut v = chunk.to_vec();
                v.resize(frame_samples, 0);
                v
            } else {
                chunk.to_vec()
            };

            match self.encoder.encode(&samples, &mut out_buf) {
                Ok(len) => {
                    self.granule_pos += granule_increment;
                    let end_info = if idx == last_idx {
                        ogg::writing::PacketWriteEndInfo::EndPage
                    } else {
                        ogg::writing::PacketWriteEndInfo::NormalPacket
                    };
                    let _ = self.writer.write_packet(
                        Cow::Owned(out_buf[..len].to_vec()),
                        self.serial,
                        end_info,
                        self.granule_pos,
                    );
                }
                Err(e) => warn!("opus encode: {}", e),
            }
        }
        Ok(())
    }

    fn finalize(mut self) {
        let _ = self.writer.write_packet(
            Cow::Owned(vec![]),
            self.serial,
            ogg::writing::PacketWriteEndInfo::EndStream,
            self.granule_pos,
        );
        let dur = self.granule_pos as f64 / 48000.0;
        info!("Recording finalized: {:.1}s {}Hz OGG/Opus", dur, self.sample_rate);
    }
}

fn interleave(user: &[i16], agent: &[i16], mode: RecordingMode) -> Vec<i16> {
    let n = user.len().max(agent.len());
    match mode {
        RecordingMode::Stereo => {
            let mut out = Vec::with_capacity(n * 2);
            for i in 0..n {
                out.push(if i < user.len() { user[i] } else { 0 });
                out.push(if i < agent.len() { agent[i] } else { 0 });
            }
            out
        }
        RecordingMode::Mono => {
            let mut out = Vec::with_capacity(n);
            for i in 0..n {
                let u = if i < user.len() { user[i] as i32 } else { 0 };
                let a = if i < agent.len() { agent[i] as i32 } else { 0 };
                out.push(((u + a) / 2).clamp(-32768, 32767) as i16);
            }
            out
        }
    }
}

// ─── RecordingManager ───────────────────────────────────────────────────────

/// Shared recording manager. Single encoder thread for all calls.
/// Used by both SipEndpoint and AudioStreamEndpoint.
pub(crate) struct RecordingManager {
    recordings: Mutex<HashMap<String, (Arc<CallRecorder>, String)>>,
    shutdown: Arc<Mutex<bool>>,
    /// Wakes encoder thread immediately (for stop/shutdown instead of waiting 2.5s).
    wake: Arc<(Mutex<bool>, Condvar)>,
}

impl RecordingManager {
    pub fn new() -> Arc<Self> {
        let mgr = Arc::new(Self {
            recordings: Mutex::new(HashMap::new()),
            shutdown: Arc::new(Mutex::new(false)),
            wake: Arc::new((Mutex::new(false), Condvar::new())),
        });

        let mgr_ref = mgr.clone();
        std::thread::Builder::new()
            .name("recording-encoder".into())
            .spawn(move || {
                if let Err(e) = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| mgr_ref.encoder_loop())) {
                    tracing::error!("Recording encoder thread panicked: {:?}", e);
                }
            })
            .expect("spawn recording encoder");

        mgr
    }

    pub fn start(&self, call_id: &str, path: &str, mode: RecordingMode, sample_rate: u32) -> Arc<CallRecorder> {
        let rec = Arc::new(CallRecorder::new(sample_rate, mode));
        self.recordings.lock().unwrap().insert(
            call_id.to_string(), (rec.clone(), path.to_string()),
        );
        info!("Recording: {} → {} ({:?} {}Hz)", call_id, path, mode, sample_rate);
        rec
    }

    pub fn stop(&self, call_id: &str) {
        if let Some((rec, _)) = self.recordings.lock().unwrap().get(call_id) {
            rec.mark_finalized();
        }
        // Wake encoder thread to do final drain + finalize immediately
        let (lock, cvar) = &*self.wake;
        *lock.lock().unwrap() = true;
        cvar.notify_one();
    }

    pub fn shutdown(&self) {
        *self.shutdown.lock().unwrap() = true;
        let (lock, cvar) = &*self.wake;
        *lock.lock().unwrap() = true;
        cvar.notify_one();
    }

    fn encoder_loop(&self) {
        let mut ogg_states: HashMap<String, OggState> = HashMap::new();

        loop {
            // Sleep up to BATCH_INTERVAL_MS, but wake immediately on stop/shutdown
            {
                let (lock, cvar) = &*self.wake;
                let mut woken = lock.lock().unwrap();
                if !*woken {
                    let (guard, _) = cvar.wait_timeout(woken, Duration::from_millis(BATCH_INTERVAL_MS)).unwrap();
                    woken = guard;
                }
                *woken = false;
            }

            let is_shutdown = *self.shutdown.lock().unwrap();
            let snapshot: Vec<(String, Arc<CallRecorder>, String)> = {
                self.recordings.lock().unwrap()
                    .iter()
                    .map(|(k, (r, p))| (k.clone(), r.clone(), p.clone()))
                    .collect()
            };

            let mut to_remove = Vec::new();

            for (call_id, recorder, path) in &snapshot {
                let (user, agent, sr, mode) = recorder.drain();

                if !ogg_states.contains_key(call_id) && (!user.is_empty() || !agent.is_empty()) {
                    match OggState::new(path, sr, mode) {
                        Ok(s) => { ogg_states.insert(call_id.clone(), s); }
                        Err(e) => { error!("OGG {}: {}", call_id, e); continue; }
                    }
                }

                if let Some(ogg) = ogg_states.get_mut(call_id) {
                    let interleaved = interleave(&user, &agent, mode);
                    if let Err(e) = ogg.encode_batch(&interleaved) {
                        error!("Encode {}: {}", call_id, e);
                    }
                }

                if recorder.is_finalized() {
                    to_remove.push(call_id.clone());
                }
            }

            for id in &to_remove {
                if let Some(ogg) = ogg_states.remove(id) { ogg.finalize(); }
                self.recordings.lock().unwrap().remove(id);
            }

            if is_shutdown {
                for (_, ogg) in ogg_states.drain() { ogg.finalize(); }
                break;
            }
        }
    }
}

impl Drop for RecordingManager {
    fn drop(&mut self) { self.shutdown(); }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn test_ogg_opus_recording() {
        let path = "/tmp/agent_transport_test_recording.ogg";

        // Clean up from previous run
        let _ = std::fs::remove_file(path);

        let mgr = RecordingManager::new();
        let rec = mgr.start("test-call", path, RecordingMode::Stereo, 16000);

        // Generate 3 seconds of test audio (440Hz user, 880Hz agent)
        let sr = 16000.0_f64;
        let chunk_size = 320; // 20ms at 16kHz
        let total_chunks = 150; // 3 seconds

        for chunk_idx in 0..total_chunks {
            let mut user = Vec::with_capacity(chunk_size);
            let mut agent = Vec::with_capacity(chunk_size);
            for i in 0..chunk_size {
                let t = (chunk_idx * chunk_size + i) as f64 / sr;
                user.push(((2.0 * std::f64::consts::PI * 440.0 * t).sin() * 16000.0) as i16);
                agent.push(((2.0 * std::f64::consts::PI * 880.0 * t).sin() * 16000.0) as i16);
            }
            rec.write_user_samples(&user);
            rec.write_agent_samples(&agent);
        }

        // Wait for encoder batch (2.5s interval)
        std::thread::sleep(Duration::from_millis(3000));

        // Stop recording — triggers final flush
        mgr.stop("test-call");

        // Wait for final batch
        std::thread::sleep(Duration::from_millis(3000));

        // Shutdown encoder thread
        mgr.shutdown();
        std::thread::sleep(Duration::from_millis(500));

        // Verify output
        let p = Path::new(path);
        assert!(p.exists(), "OGG file should exist");

        let data = std::fs::read(p).unwrap();
        assert!(data.len() > 100, "OGG file should have content (got {} bytes)", data.len());
        assert_eq!(&data[0..4], b"OggS", "Should have OGG magic header");

        // Check for Opus headers
        let content = String::from_utf8_lossy(&data);
        assert!(content.contains("OpusHead"), "Should contain OpusHead");
        assert!(content.contains("OpusTags"), "Should contain OpusTags");
        assert!(content.contains("agent-transport"), "Should contain vendor string");

        println!("OGG file: {} bytes ({:.1} KB)", data.len(), data.len() as f64 / 1024.0);
        assert!(data.len() > 1000, "OGG file should have real audio data, not just headers (got {} bytes)", data.len());

        // Clean up
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn test_late_binding_recorder() {
        // Verify the Arc<Mutex<Option>> pattern works
        let recorder: Arc<Mutex<Option<Arc<CallRecorder>>>> = Arc::new(Mutex::new(None));

        // Clone for "send loop"
        let rec_ref = recorder.clone();

        // Initially empty
        assert!(rec_ref.lock().unwrap().is_none());

        // "start_recording" sets it later
        let mgr = RecordingManager::new();
        let call_rec = mgr.start("late-test", "/tmp/late_test.ogg", RecordingMode::Mono, 16000);
        *recorder.lock().unwrap() = Some(call_rec);

        // Now the "send loop" can see it
        assert!(rec_ref.lock().unwrap().is_some());

        // Write some samples through the shared reference
        if let Some(ref rec) = *rec_ref.lock().unwrap() {
            rec.write_agent_samples(&[100, 200, 300]);
            rec.write_user_samples(&[400, 500, 600]);
        }

        // Cleanup
        mgr.stop("late-test");
        mgr.shutdown();
        std::thread::sleep(Duration::from_millis(3500));
        let _ = std::fs::remove_file("/tmp/late_test.ogg");
    }
}

