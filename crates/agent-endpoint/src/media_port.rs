//! Custom media port for bridging audio frames in/out of the conference bridge.
//!
//! This creates a media port with custom `get_frame` and `put_frame` callbacks.
//! - `put_frame` is called when audio arrives from the remote party -> `recv_audio()`
//! - `get_frame` is called when audio is needed to send to the remote -> `send_audio()`

use std::ffi::CString;
use std::os::raw::c_void;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use crossbeam_channel::{Receiver, Sender};

use pjsua_sys::*;

use beep_detector::BeepDetectorResult;

use crate::audio::AudioFrame;
use crate::events::EndpointEvent;

/// Internal state attached to each custom media port via `port_data.pdata`.
struct PortUserData {
    /// Frames from user → sent to remote (get_frame reads from here)
    outgoing_rx: Receiver<Vec<i16>>,
    /// Frames from remote → available to user (put_frame writes here)
    incoming_tx: Sender<AudioFrame>,
    /// Event sender for beep detection events
    event_tx: Sender<EndpointEvent>,
    /// Call ID (needed to look up beep detector in global state)
    call_id: i32,
    /// Shared flush flag — when true, drain outgoing queue and output silence
    flush_flag: Arc<AtomicBool>,
    /// Shared pause flag — when true, output silence without draining
    pause_flag: Arc<AtomicBool>,
    /// Shared playout notification — signaled when queue drains empty
    playout_notify: Arc<(std::sync::Mutex<bool>, std::sync::Condvar)>,
    /// Clock rate of this port
    clock_rate: u32,
    /// Channels
    channels: u32,
    /// Samples per frame
    samples_per_frame: u32,
}

/// A handle to a custom media port registered with the conference bridge.
pub struct CustomMediaPort {
    /// Raw pointer to the underlying media port
    port: *mut pjmedia_port,
    /// Pool used to register the port (must be released on destroy)
    pool: *mut pj_pool_t,
    /// Conference bridge slot ID
    pub conf_slot: i32,
    /// Send audio frames for outgoing (to remote)
    outgoing_tx: Sender<Vec<i16>>,
    /// Receive audio frames from incoming (from remote)
    incoming_rx: Receiver<AudioFrame>,
    /// Shared: when true, get_frame drains queue and outputs silence
    flush_flag: Arc<AtomicBool>,
    /// Shared: when true, get_frame outputs silence without draining
    pause_flag: Arc<AtomicBool>,
    /// Shared: signals that the outgoing queue is fully drained (for wait_for_playout)
    playout_notify: Arc<(std::sync::Mutex<bool>, std::sync::Condvar)>,
    /// Clock rate
    pub clock_rate: u32,
    /// Samples per frame
    pub samples_per_frame: u32,
}

impl CustomMediaPort {
    /// Create a new custom media port and register it with the conference bridge.
    ///
    /// `clock_rate`: e.g. 48000 for Opus, 8000 for PCMU
    /// `channels`: 1 for mono
    /// `samples_per_frame`: e.g. 960 for 20ms @ 48kHz
    pub unsafe fn new(
        call_id: i32,
        clock_rate: u32,
        channels: u32,
        samples_per_frame: u32,
        event_tx: Sender<EndpointEvent>,
    ) -> Result<Self, i32> {
        // Create channels for audio frame passing
        let (outgoing_tx, outgoing_rx) = crossbeam_channel::bounded(50);
        let (incoming_tx, incoming_rx) = crossbeam_channel::bounded(50);
        let flush_flag = Arc::new(AtomicBool::new(false));
        let pause_flag = Arc::new(AtomicBool::new(false));
        let playout_notify = Arc::new((std::sync::Mutex::new(false), std::sync::Condvar::new()));

        // Allocate user data on heap
        let user_data = Box::new(PortUserData {
            outgoing_rx,
            incoming_tx,
            event_tx,
            call_id,
            flush_flag: flush_flag.clone(),
            pause_flag: pause_flag.clone(),
            playout_notify: playout_notify.clone(),
            clock_rate,
            channels,
            samples_per_frame,
        });
        let user_data_ptr = Box::into_raw(user_data) as *mut c_void;

        // Allocate the port on heap
        let port = Box::new(pjmedia_port::default());
        let port_ptr = Box::into_raw(port);

        // Initialize port info
        let name = CString::new("agent-audio").unwrap();
        let pj_name = pj_str_t {
            ptr: name.as_ptr() as *mut _,
            slen: name.as_bytes().len() as _,
        };

        let status = pjmedia_port_info_init(
            &mut (*port_ptr).info,
            &pj_name,
            0x41474E54, // "AGNT" signature
            clock_rate,
            channels,
            16, // bits per sample (PCM16)
            samples_per_frame,
        );
        if status != pj_constants__PJ_SUCCESS as i32 {
            // Clean up
            let _ = Box::from_raw(user_data_ptr as *mut PortUserData);
            let _ = Box::from_raw(port_ptr);
            return Err(status);
        }

        // Set callbacks
        (*port_ptr).put_frame = Some(port_put_frame);
        (*port_ptr).get_frame = Some(port_get_frame);
        (*port_ptr).on_destroy = Some(port_on_destroy);
        (*port_ptr).port_data.pdata = user_data_ptr;

        // Keep name alive — leak it since the port info struct holds a reference
        std::mem::forget(name);

        // Add to conference bridge
        let pool = pjsua_pool_create(
            b"agent-port\0".as_ptr() as *const _,
            1024,
            1024,
        );
        let mut conf_slot: pjsua_conf_port_id = -1;
        let status = pjsua_conf_add_port(pool, port_ptr, &mut conf_slot);
        if status != pj_constants__PJ_SUCCESS as i32 {
            let _ = Box::from_raw(user_data_ptr as *mut PortUserData);
            let _ = Box::from_raw(port_ptr);
            return Err(status);
        }

        Ok(Self {
            port: port_ptr,
            pool,
            conf_slot,
            flush_flag,
            pause_flag,
            playout_notify,
            outgoing_tx,
            incoming_rx,
            clock_rate,
            samples_per_frame,
        })
    }

    /// Connect this port to a call's conference slot (bidirectional audio).
    pub unsafe fn connect_to_call(&self, call_conf_slot: i32) -> Result<(), i32> {
        // Our port → call (send audio to remote)
        let status = pjsua_conf_connect(self.conf_slot, call_conf_slot);
        if status != pj_constants__PJ_SUCCESS as i32 {
            return Err(status);
        }
        // Call → our port (receive audio from remote)
        let status = pjsua_conf_connect(call_conf_slot, self.conf_slot);
        if status != pj_constants__PJ_SUCCESS as i32 {
            return Err(status);
        }
        Ok(())
    }

    /// Disconnect this port from a call's conference slot.
    pub unsafe fn disconnect_from_call(&self, call_conf_slot: i32) {
        pjsua_conf_disconnect(self.conf_slot, call_conf_slot);
        pjsua_conf_disconnect(call_conf_slot, self.conf_slot);
    }

    /// Queue an audio frame to be sent to the remote party.
    pub fn send_audio(&self, frame: &AudioFrame) -> bool {
        self.outgoing_tx.try_send(frame.data.clone()).is_ok()
    }

    /// Mark the current playback segment as complete.
    /// Stops accepting new frames and lets any remaining queued audio play out.
    /// After the queue drains, the port outputs silence.
    pub fn flush(&self) {
        // Nothing to actively do — the queue will drain naturally.
        // The sender side should stop sending after calling flush.
        // Signal playout tracking so wait_for_playout can observe drain.
        let (lock, _) = &*self.playout_notify;
        if let Ok(mut done) = lock.lock() {
            *done = false; // reset — will be set true when queue drains
        }
    }

    /// Clear all queued outgoing audio immediately. Discards any frames
    /// that haven't been sent yet and outputs silence.
    /// Use this when the agent is interrupted mid-speech (barge-in).
    pub fn clear_buffer(&self) {
        self.flush_flag.store(true, Ordering::Relaxed);
    }

    /// Block until all queued outgoing audio has been played out.
    /// Returns once the outgoing queue is empty.
    pub fn wait_for_playout(&self, timeout_ms: u64) -> bool {
        let (lock, cvar) = &*self.playout_notify;
        let guard = lock.lock().unwrap();
        let result = cvar
            .wait_timeout_while(guard, std::time::Duration::from_millis(timeout_ms), |done| !*done)
            .unwrap();
        !result.1.timed_out()
    }

    /// Pause audio playback. Queued frames are preserved but silence is output.
    pub fn pause(&self) {
        self.pause_flag.store(true, Ordering::Relaxed);
    }

    /// Resume audio playback after a pause.
    pub fn resume(&self) {
        self.pause_flag.store(false, Ordering::Relaxed);
    }

    /// Receive an audio frame from the remote party (non-blocking).
    pub fn recv_audio(&self) -> Option<AudioFrame> {
        self.incoming_rx.try_recv().ok()
    }

    /// Remove from conference bridge and release the pool.
    pub unsafe fn destroy(&self) {
        pjsua_conf_remove_port(self.conf_slot);
        if !self.pool.is_null() {
            pj_pool_release(self.pool);
        }
    }
}

// ─── Media port callbacks ────────────────────────────────────────────────────

/// Called when the conference bridge needs audio to send to the remote party.
/// We read from the outgoing channel (fed by `send_audio()`).
unsafe extern "C" fn port_get_frame(
    this_port: *mut pjmedia_port,
    frame: *mut pjmedia_frame,
) -> pj_status_t {
    let ud = (*this_port).port_data.pdata as *mut PortUserData;
    if ud.is_null() || frame.is_null() {
        return pj_constants__PJ_SUCCESS as pj_status_t;
    }

    let user_data = &*ud;
    let buf = (*frame).buf as *mut i16;
    let buf_samples = user_data.samples_per_frame as usize;

    // clear_buffer: drain all queued frames and output silence
    if user_data.flush_flag.swap(false, Ordering::Relaxed) {
        while user_data.outgoing_rx.try_recv().is_ok() {}
        std::ptr::write_bytes(buf, 0, buf_samples);
        (*frame).type_ = pjmedia_frame_type_PJMEDIA_FRAME_TYPE_NONE;
        (*frame).size = (buf_samples * 2) as pj_size_t;
        // Notify wait_for_playout that queue is empty
        if let Ok(mut done) = user_data.playout_notify.0.lock() {
            *done = true;
            user_data.playout_notify.1.notify_all();
        }
        return pj_constants__PJ_SUCCESS as pj_status_t;
    }

    // pause: output silence but keep the queue intact
    if user_data.pause_flag.load(Ordering::Relaxed) {
        std::ptr::write_bytes(buf, 0, buf_samples);
        (*frame).type_ = pjmedia_frame_type_PJMEDIA_FRAME_TYPE_NONE;
        (*frame).size = (buf_samples * 2) as pj_size_t;
        return pj_constants__PJ_SUCCESS as pj_status_t;
    }

    match user_data.outgoing_rx.try_recv() {
        Ok(samples) => {
            let copy_len = samples.len().min(buf_samples);
            std::ptr::copy_nonoverlapping(samples.as_ptr(), buf, copy_len);
            if copy_len < buf_samples {
                std::ptr::write_bytes(buf.add(copy_len), 0, buf_samples - copy_len);
            }
            (*frame).type_ = pjmedia_frame_type_PJMEDIA_FRAME_TYPE_AUDIO;
            (*frame).size = (buf_samples * 2) as pj_size_t;
        }
        Err(_) => {
            // Queue empty — output silence and notify wait_for_playout
            std::ptr::write_bytes(buf, 0, buf_samples);
            (*frame).type_ = pjmedia_frame_type_PJMEDIA_FRAME_TYPE_NONE;
            (*frame).size = (buf_samples * 2) as pj_size_t;
            if let Ok(mut done) = user_data.playout_notify.0.lock() {
                *done = true;
                user_data.playout_notify.1.notify_all();
            }
        }
    }

    pj_constants__PJ_SUCCESS as pj_status_t
}

/// Called when audio arrives from the remote party.
/// We write into the incoming channel (consumed by `recv_audio()`).
unsafe extern "C" fn port_put_frame(
    this_port: *mut pjmedia_port,
    frame: *mut pjmedia_frame,
) -> pj_status_t {
    let ud = (*this_port).port_data.pdata as *mut PortUserData;
    if ud.is_null() || frame.is_null() {
        return pj_constants__PJ_SUCCESS as pj_status_t;
    }

    let user_data = &*ud;

    if (*frame).type_ == pjmedia_frame_type_PJMEDIA_FRAME_TYPE_AUDIO && (*frame).size > 0 {
        let buf = (*frame).buf as *const i16;
        let num_samples = ((*frame).size as usize) / 2; // 2 bytes per i16

        let mut data = vec![0i16; num_samples];
        std::ptr::copy_nonoverlapping(buf, data.as_mut_ptr(), num_samples);

        // Feed beep detector if active for this call (before moving data)
        let call_id = user_data.call_id;
        if let Ok(mut state) = crate::endpoint::global_state().lock() {
            if let Some(detector) = state.beep_detectors.get_mut(&call_id) {
                match detector.process_frame(&data) {
                    BeepDetectorResult::Detected(event) => {
                        let _ = user_data.event_tx.try_send(EndpointEvent::BeepDetected {
                            call_id,
                            frequency_hz: event.frequency_hz,
                            duration_ms: event.duration_ms,
                        });
                        // Auto-remove detector after it fires
                        state.beep_detectors.remove(&call_id);
                    }
                    BeepDetectorResult::Timeout => {
                        let _ = user_data.event_tx.try_send(EndpointEvent::BeepTimeout {
                            call_id,
                        });
                        state.beep_detectors.remove(&call_id);
                    }
                    BeepDetectorResult::Listening => {}
                }
            }
        }

        // Now create the AudioFrame and send to recv_audio channel
        let audio_frame = AudioFrame {
            data,
            sample_rate: user_data.clock_rate,
            num_channels: user_data.channels,
            samples_per_channel: num_samples as u32 / user_data.channels,
        };
        let _ = user_data.incoming_tx.try_send(audio_frame);
    }

    pj_constants__PJ_SUCCESS as pj_status_t
}

/// Called when the port is destroyed — clean up heap allocations.
unsafe extern "C" fn port_on_destroy(this_port: *mut pjmedia_port) -> pj_status_t {
    let ud = (*this_port).port_data.pdata as *mut PortUserData;
    if !ud.is_null() {
        // Reclaim the user data
        let _ = Box::from_raw(ud);
        (*this_port).port_data.pdata = std::ptr::null_mut();
    }
    pj_constants__PJ_SUCCESS as pj_status_t
}
