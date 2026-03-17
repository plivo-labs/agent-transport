use std::collections::HashMap;
use std::ffi::CString;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, Once};

use crossbeam_channel::{Receiver, Sender};
use tracing::{debug, error, info, warn};

use crate::audio::AudioFrame;
use crate::beep::{BeepDetector, BeepDetectorConfig, BeepDetectorResult};
use crate::call::{CallDirection, CallSession, CallState};
use crate::config::{Codec, EndpointConfig};
use crate::error::{EndpointError, Result};
use crate::events::EndpointEvent;
use crate::media_port::CustomMediaPort;

use pjsua_sys::*;

/// Global state for endpoint callbacks (only one endpoint may exist per process).
static INIT: Once = Once::new();

pub(crate) struct GlobalState {
    event_tx: Sender<EndpointEvent>,
    calls: HashMap<i32, CallSession>,
    /// Per-call custom media ports for audio I/O
    media_ports: HashMap<i32, CustomMediaPort>,
    /// Per-call WAV recorders (recorder_id)
    recorders: HashMap<i32, pjsua_recorder_id>,
    /// Per-call beep detectors (running async on incoming audio)
    pub(crate) beep_detectors: HashMap<i32, BeepDetector>,
    /// Conference bridge clock rate (set during init)
    clock_rate: u32,
}

static mut GLOBAL: Option<Mutex<GlobalState>> = None;

pub(crate) fn global_state() -> &'static Mutex<GlobalState> {
    // SAFETY: GLOBAL is only written once inside INIT.call_once() and read-only after.
    #[allow(static_mut_refs)]
    unsafe {
        GLOBAL.as_ref().expect("endpoint not initialized")
    }
}

/// The SIP endpoint — provides call control and audio I/O.
///
/// Only one instance may exist per process.
pub struct SipEndpoint {
    config: EndpointConfig,
    account_id: Mutex<Option<pjsua_acc_id>>,
    initialized: AtomicBool,
    event_rx: Receiver<EndpointEvent>,
}

impl SipEndpoint {
    /// Create and initialize a new SIP endpoint.
    ///
    /// This sets up the SIP stack, creates a UDP transport, and configures codecs.
    /// Only one SipEndpoint may exist per process.
    pub fn new(config: EndpointConfig) -> Result<Self> {
        let (event_tx, event_rx) = crossbeam_channel::unbounded();

        // Initialize global state (only once)
        let mut already_initialized = true;
        INIT.call_once(|| {
            #[allow(static_mut_refs)]
            unsafe {
                GLOBAL = Some(Mutex::new(GlobalState {
                    event_tx,
                    calls: HashMap::new(),
                    media_ports: HashMap::new(),
                    recorders: HashMap::new(),
                    beep_detectors: HashMap::new(),
                    clock_rate: 8000,
                }));
            }
            already_initialized = false;
        });

        if already_initialized {
            return Err(EndpointError::AlreadyInitialized);
        }

        unsafe {
            Self::init_pjsua(&config)?;
        }

        Ok(Self {
            config,
            account_id: Mutex::new(None),
            initialized: AtomicBool::new(true),
            event_rx,
        })
    }

    unsafe fn init_pjsua(config: &EndpointConfig) -> Result<()> {
        // Create SIP stack instance
        let status = pjsua_create();
        check_status(status, "pjsua_create")?;

        // Configure SIP stack
        let mut cfg: pjsua_config = std::mem::zeroed();
        pjsua_config_default(&mut cfg);

        // Set callbacks
        cfg.cb.on_reg_state2 = Some(on_reg_state);
        cfg.cb.on_incoming_call = Some(on_incoming_call);
        cfg.cb.on_call_state = Some(on_call_state);
        cfg.cb.on_call_media_state = Some(on_call_media_state);
        cfg.cb.on_dtmf_digit2 = Some(on_dtmf_digit2);
        cfg.cb.on_call_transfer_status = Some(on_call_transfer_status);

        // User-Agent
        let ua_cstr = CString::new(config.user_agent.as_str()).unwrap();
        cfg.user_agent = pj_str_from_cstr(&ua_cstr);

        // STUN — primary + Google fallback
        let stun_cstr = CString::new(config.stun_server.as_str()).unwrap();
        let stun_fallback = CString::new("stun.l.google.com:19302").unwrap();
        cfg.stun_srv_cnt = 2;
        cfg.stun_srv[0] = pj_str_from_cstr(&stun_cstr);
        cfg.stun_srv[1] = pj_str_from_cstr(&stun_fallback);

        // Logging
        let mut log_cfg: pjsua_logging_config = std::mem::zeroed();
        pjsua_logging_config_default(&mut log_cfg);
        log_cfg.level = config.log_level;
        log_cfg.console_level = config.log_level;

        // Media config
        let mut media_cfg: pjsua_media_config = std::mem::zeroed();
        pjsua_media_config_default(&mut media_cfg);
        media_cfg.enable_ice = if config.enable_ice {
            pj_constants__PJ_TRUE as i32
        } else {
            pj_constants__PJ_FALSE as i32
        };
        media_cfg.ice_always_update = pj_constants__PJ_TRUE as i32;
        media_cfg.no_vad = pj_constants__PJ_TRUE as i32;
        // 16kHz bridge clock rate — resampled from codec native rate (8kHz PCMU/PCMA).
        // Agent pipelines (LiveKit, Pipecat) work best at 16kHz.
        media_cfg.clock_rate = 16000;
        media_cfg.snd_clock_rate = 16000;

        // Initialize
        let status = pjsua_init(&cfg, &log_cfg, &media_cfg);
        check_status(status, "pjsua_init")?;

        // Create UDP transport
        let mut transport_cfg: pjsua_transport_config = std::mem::zeroed();
        pjsua_transport_config_default(&mut transport_cfg);
        transport_cfg.port = config.local_port as u32;

        let mut transport_id: pjsua_transport_id = -1;
        let status = pjsua_transport_create(
            pjsip_transport_type_e_PJSIP_TRANSPORT_UDP,
            &transport_cfg,
            &mut transport_id,
        );
        check_status(status, "pjsua_transport_create")?;

        // Set null sound device (no physical audio device needed)
        let status = pjsua_set_null_snd_dev();
        check_status(status, "pjsua_set_null_snd_dev")?;

        // Start SIP stack
        let status = pjsua_start();
        check_status(status, "pjsua_start")?;

        // Configure codec priorities
        Self::configure_codecs(&config.codecs)?;

        if let Ok(mut state) = global_state().lock() {
            state.clock_rate = 16000;
        }

        info!("Agent endpoint initialized");
        Ok(())
    }

    unsafe fn configure_codecs(codecs: &[Codec]) -> Result<()> {
        // Disable all codecs first
        let all_codecs = [Codec::Opus, Codec::PCMU, Codec::PCMA, Codec::G722];
        for codec in &all_codecs {
            let name = CString::new(codec.pjsua_name()).unwrap();
            let mut pj_name = pj_str_from_cstr(&name);
            pjsua_codec_set_priority(&mut pj_name, 0);
        }

        // Enable requested codecs in priority order (highest first)
        for (i, codec) in codecs.iter().enumerate() {
            let name = CString::new(codec.pjsua_name()).unwrap();
            let mut pj_name = pj_str_from_cstr(&name);
            let priority = (255 - i as u8) as u8;
            let status = pjsua_codec_set_priority(&mut pj_name, priority);
            if status != pj_constants__PJ_SUCCESS as i32 {
                warn!("Failed to set priority for codec {:?}: {}", codec, status);
            }
        }

        Ok(())
    }

    /// Register with the SIP server using digest authentication.
    pub fn register(&self, username: &str, password: &str) -> Result<()> {
        if !self.initialized.load(Ordering::Relaxed) {
            return Err(EndpointError::NotInitialized);
        }

        let sip_uri = format!("sip:{}@{}", username, self.config.sip_server);
        let registrar = format!("sip:{}", self.config.sip_server);

        let id_cstr = CString::new(sip_uri.as_str()).unwrap();
        let reg_cstr = CString::new(registrar.as_str()).unwrap();
        let realm_cstr = CString::new("*").unwrap();
        let user_cstr = CString::new(username).unwrap();
        let pass_cstr = CString::new(password).unwrap();
        let scheme_cstr = CString::new("digest").unwrap();

        unsafe {
            let mut acc_cfg: pjsua_acc_config = std::mem::zeroed();
            pjsua_acc_config_default(&mut acc_cfg);

            acc_cfg.id = pj_str_from_cstr(&id_cstr);
            acc_cfg.reg_uri = pj_str_from_cstr(&reg_cstr);
            acc_cfg.reg_timeout = self.config.register_expires;

            // Credentials — all CStrings must outlive the account registration call
            acc_cfg.cred_count = 1;
            acc_cfg.cred_info[0].realm = pj_str_from_cstr(&realm_cstr);
            acc_cfg.cred_info[0].scheme = pj_str_from_cstr(&scheme_cstr);
            acc_cfg.cred_info[0].username = pj_str_from_cstr(&user_cstr);
            acc_cfg.cred_info[0].data = pj_str_from_cstr(&pass_cstr);
            acc_cfg.cred_info[0].data_type =
                pjsip_cred_data_type_PJSIP_CRED_DATA_PLAIN_PASSWD as i32;

            // NAT / STUN / ICE for this account
            if self.config.enable_ice {
                acc_cfg.ice_cfg_use = pjsua_ice_config_use_PJSUA_ICE_CONFIG_USE_DEFAULT;
            }

            // TURN relay for symmetric NAT traversal
            if let Some(ref turn) = self.config.turn_server {
                let turn_server_cstr = CString::new(turn.server.as_str()).unwrap();
                let turn_user_cstr = CString::new(turn.username.as_str()).unwrap();
                let turn_pass_cstr = CString::new(turn.password.as_str()).unwrap();

                acc_cfg.turn_cfg_use =
                    pjsua_turn_config_use_PJSUA_TURN_CONFIG_USE_CUSTOM;
                acc_cfg.turn_cfg.enable_turn = pj_constants__PJ_TRUE as i32;
                acc_cfg.turn_cfg.turn_server = pj_str_from_cstr(&turn_server_cstr);
                acc_cfg.turn_cfg.turn_conn_type = 17; // PJ_TURN_TP_UDP
                acc_cfg.turn_cfg.turn_auth_cred.type_ =
                    pj_stun_auth_cred_type_PJ_STUN_AUTH_CRED_STATIC;
                acc_cfg.turn_cfg.turn_auth_cred.data.static_cred.username =
                    pj_str_from_cstr(&turn_user_cstr);
                acc_cfg.turn_cfg.turn_auth_cred.data.static_cred.data =
                    pj_str_from_cstr(&turn_pass_cstr);
                acc_cfg.turn_cfg.turn_auth_cred.data.static_cred.data_type = 0; // plaintext

                // Keep CStrings alive until pjsua_acc_add copies them
                std::mem::forget(turn_server_cstr);
                std::mem::forget(turn_user_cstr);
                std::mem::forget(turn_pass_cstr);

                info!("TURN relay configured: {}", turn.server);
            }

            // SRTP
            if self.config.enable_srtp {
                acc_cfg.use_srtp =
                    pjmedia_srtp_use_PJMEDIA_SRTP_OPTIONAL;
            }

            let mut acc_id: pjsua_acc_id = -1;
            let status = pjsua_acc_add(&acc_cfg, pj_constants__PJ_TRUE as i32, &mut acc_id);
            check_status(status, "pjsua_acc_add")?;

            *self.account_id.lock().unwrap() = Some(acc_id);
            info!("SIP account registered: {} (acc_id={})", sip_uri, acc_id);
        }

        Ok(())
    }

    /// Unregister from the SIP server.
    pub fn unregister(&self) -> Result<()> {
        let acc_id = self
            .account_id
            .lock()
            .unwrap()
            .take()
            .ok_or(EndpointError::NotRegistered)?;

        unsafe {
            let status = pjsua_acc_del(acc_id);
            check_status(status, "pjsua_acc_del")?;
        }

        info!("SIP account unregistered");
        Ok(())
    }

    /// Check if currently registered.
    pub fn is_registered(&self) -> bool {
        let acc_id = match *self.account_id.lock().unwrap() {
            Some(id) => id,
            None => return false,
        };
        unsafe { pjsua_acc_is_valid(acc_id) != 0 }
    }

    /// Make an outbound call.
    ///
    /// Returns the call ID.
    pub fn call(
        &self,
        dest_uri: &str,
        _headers: Option<HashMap<String, String>>,
    ) -> Result<i32> {
        let acc_id = self
            .account_id
            .lock()
            .unwrap()
            .ok_or(EndpointError::NotRegistered)?;

        let uri_cstr = CString::new(dest_uri).unwrap();

        unsafe {
            let mut call_id: pjsua_call_id = -1;
            let uri = pj_str_from_cstr(&uri_cstr);

            // TODO: add custom headers via msg_data if provided

            let status = pjsua_call_make_call(
                acc_id,
                &uri,
                std::ptr::null(),  // call_setting
                std::ptr::null_mut(), // user_data
                std::ptr::null(),  // msg_data
                &mut call_id,
            );
            check_status(status, "pjsua_call_make_call")?;

            // Create call session
            let session = CallSession::new(call_id, CallDirection::Outbound);
            {
                let mut state = global_state().lock().unwrap();
                state.calls.insert(call_id, session);
                // Media port is created when media becomes active (on_call_media_state)
            }

            info!("Outbound call initiated: {} (call_id={})", dest_uri, call_id);
            Ok(call_id)
        }
    }

    /// Answer an incoming call.
    ///
    /// Use code 200 to accept, 180 to ring.
    pub fn answer(&self, call_id: i32, code: u16) -> Result<()> {
        unsafe {
            let status = pjsua_call_answer(call_id, code as u32, std::ptr::null(), std::ptr::null());
            check_status(status, "pjsua_call_answer")?;
        }
        info!("Call {} answered with code {}", call_id, code);
        Ok(())
    }

    /// Reject an incoming call.
    pub fn reject(&self, call_id: i32, code: u16) -> Result<()> {
        unsafe {
            let status =
                pjsua_call_answer(call_id, code as u32, std::ptr::null(), std::ptr::null());
            check_status(status, "pjsua_call_answer (reject)")?;
        }
        info!("Call {} rejected with code {}", call_id, code);
        Ok(())
    }

    /// Hang up an active call.
    pub fn hangup(&self, call_id: i32) -> Result<()> {
        unsafe {
            let status = pjsua_call_hangup(call_id, 0, std::ptr::null(), std::ptr::null());
            check_status(status, "pjsua_call_hangup")?;
        }
        info!("Call {} hung up", call_id);
        Ok(())
    }

    /// Send DTMF digits using the specified method.
    ///
    /// `method`: "rfc2833" (default), "sip_info", or "auto"
    pub fn send_dtmf(&self, call_id: i32, digits: &str) -> Result<()> {
        self.send_dtmf_with_method(call_id, digits, "rfc2833")
    }

    /// Send DTMF digits with explicit method selection.
    ///
    /// Methods: "rfc2833" (RTP events), "sip_info" (SIP INFO message)
    pub fn send_dtmf_with_method(&self, call_id: i32, digits: &str, method: &str) -> Result<()> {
        let digits_cstr = CString::new(digits).unwrap();
        unsafe {
            let mut param: pjsua_call_send_dtmf_param = std::mem::zeroed();
            pjsua_call_send_dtmf_param_default(&mut param);
            param.digits = pj_str_from_cstr(&digits_cstr);
            param.method = match method {
                "sip_info" | "sipinfo" | "info" => pjsua_dtmf_method_PJSUA_DTMF_METHOD_SIP_INFO,
                _ => pjsua_dtmf_method_PJSUA_DTMF_METHOD_RFC2833,
            };
            let status = pjsua_call_send_dtmf(call_id, &param);
            check_status(status, "pjsua_call_send_dtmf")?;
        }
        info!("DTMF sent on call {} ({}): {}", call_id, method, digits);
        Ok(())
    }

    /// Blind transfer via SIP REFER.
    pub fn transfer(&self, call_id: i32, dest_uri: &str) -> Result<()> {
        let uri_cstr = CString::new(dest_uri).unwrap();
        unsafe {
            let uri = pj_str_from_cstr(&uri_cstr);
            let status = pjsua_call_xfer(call_id, &uri, std::ptr::null());
            check_status(status, "pjsua_call_xfer")?;
        }
        info!("Call {} transferred to {}", call_id, dest_uri);
        Ok(())
    }

    /// Attended transfer (connect two active calls).
    pub fn transfer_attended(&self, call_id: i32, target_call_id: i32) -> Result<()> {
        unsafe {
            let status =
                pjsua_call_xfer_replaces(call_id, target_call_id, 0, std::ptr::null());
            check_status(status, "pjsua_call_xfer_replaces")?;
        }
        info!(
            "Attended transfer: call {} → call {}",
            call_id, target_call_id
        );
        Ok(())
    }

    /// Mute outgoing audio on a call (disconnect from conference bridge).
    pub fn mute(&self, call_id: i32) -> Result<()> {
        unsafe {
            let ci = get_call_info(call_id)?;
            // Disconnect our audio source from the call's conf slot
            // This effectively mutes our side
            if ci.conf_slot != pjsua_invalid_id_const__PJSUA_INVALID_ID as i32 {
                // The null sound device port is slot 0
                pjsua_conf_disconnect(0, ci.conf_slot as i32);
            }
        }
        debug!("Call {} muted", call_id);
        Ok(())
    }

    /// Mark the current playback segment as complete.
    /// Queued audio continues playing, but the segment boundary is recorded.
    pub fn flush(&self, call_id: i32) -> Result<()> {
        let state = global_state().lock().unwrap();
        if let Some(port) = state.media_ports.get(&call_id) {
            port.flush();
            Ok(())
        } else {
            Err(EndpointError::CallNotActive(call_id))
        }
    }

    /// Clear all queued outgoing audio immediately.
    /// Discards any frames that haven't been sent yet and outputs silence.
    /// Use this when the agent is interrupted mid-speech (barge-in).
    pub fn clear_buffer(&self, call_id: i32) -> Result<()> {
        let state = global_state().lock().unwrap();
        if let Some(port) = state.media_ports.get(&call_id) {
            port.clear_buffer();
            debug!("Cleared audio buffer on call {}", call_id);
            Ok(())
        } else {
            Err(EndpointError::CallNotActive(call_id))
        }
    }

    /// Block until all queued outgoing audio has finished playing.
    /// Returns true if playout completed, false if timeout.
    pub fn wait_for_playout(&self, call_id: i32, timeout_ms: u64) -> Result<bool> {
        let state = global_state().lock().unwrap();
        if let Some(port) = state.media_ports.get(&call_id) {
            let completed = port.wait_for_playout(timeout_ms);
            Ok(completed)
        } else {
            Err(EndpointError::CallNotActive(call_id))
        }
    }

    /// Pause audio playback. Queued frames are preserved but silence is output.
    pub fn pause(&self, call_id: i32) -> Result<()> {
        let state = global_state().lock().unwrap();
        if let Some(port) = state.media_ports.get(&call_id) {
            port.pause();
            Ok(())
        } else {
            Err(EndpointError::CallNotActive(call_id))
        }
    }

    /// Resume audio playback after a pause.
    pub fn resume(&self, call_id: i32) -> Result<()> {
        let state = global_state().lock().unwrap();
        if let Some(port) = state.media_ports.get(&call_id) {
            port.resume();
            Ok(())
        } else {
            Err(EndpointError::CallNotActive(call_id))
        }
    }

    /// Unmute outgoing audio on a call.
    pub fn unmute(&self, call_id: i32) -> Result<()> {
        unsafe {
            let ci = get_call_info(call_id)?;
            if ci.conf_slot != pjsua_invalid_id_const__PJSUA_INVALID_ID as i32 {
                pjsua_conf_connect(0, ci.conf_slot as i32);
            }
        }
        debug!("Call {} unmuted", call_id);
        Ok(())
    }

    /// Send an audio frame into the active call.
    ///
    /// The frame is queued and fed into the conference bridge on the next
    /// media port callback (typically every 20ms).
    pub fn send_audio(&self, call_id: i32, frame: &AudioFrame) -> Result<()> {
        let state = global_state().lock().unwrap();
        if let Some(port) = state.media_ports.get(&call_id) {
            if port.send_audio(frame) {
                Ok(())
            } else {
                Err(EndpointError::Other("audio buffer full".into()))
            }
        } else {
            Err(EndpointError::CallNotActive(call_id))
        }
    }

    /// Receive the next audio frame from a call (non-blocking).
    ///
    /// Returns None if no frame is available yet.
    pub fn recv_audio(&self, call_id: i32) -> Result<Option<AudioFrame>> {
        let state = global_state().lock().unwrap();
        if let Some(port) = state.media_ports.get(&call_id) {
            Ok(port.recv_audio())
        } else {
            Err(EndpointError::CallNotActive(call_id))
        }
    }

    /// Start recording a call to a WAV file.
    ///
    /// The file path must end in `.wav`. Recording captures the call's audio
    /// from the conference bridge. Call `stop_recording` to finalize the file.
    ///
    /// Note: The conference bridge mix is recorded as mono PCM WAV at the
    /// bridge clock rate (8000Hz for PCMU/PCMA).
    pub fn start_recording(&self, call_id: i32, path: &str) -> Result<()> {
        let path_cstr = CString::new(path).unwrap();

        unsafe {
            // Verify call is active
            let ci = get_call_info(call_id)?;

            let pj_path = pj_str_from_cstr(&path_cstr);
            let mut recorder_id: pjsua_recorder_id = -1;

            let status = pjsua_recorder_create(
                &pj_path,
                0,                      // enc_type (0 = default)
                std::ptr::null_mut(),   // enc_param
                -1,                     // max_size (-1 = unlimited)
                0,                      // options
                &mut recorder_id,
            );
            check_status(status, "pjsua_recorder_create")?;

            // Connect the call's audio to the recorder
            let rec_conf = pjsua_recorder_get_conf_port(recorder_id);
            let status = pjsua_conf_connect(ci.conf_slot as i32, rec_conf);
            check_status(status, "pjsua_conf_connect (recorder)")?;

            let mut state = global_state().lock().unwrap();
            state.recorders.insert(call_id, recorder_id);
        }

        info!("Recording call {} to {}", call_id, path);
        Ok(())
    }

    /// Stop recording a call and finalize the WAV file.
    pub fn stop_recording(&self, call_id: i32) -> Result<()> {
        let mut state = global_state().lock().unwrap();
        if let Some(recorder_id) = state.recorders.remove(&call_id) {
            unsafe {
                let status = pjsua_recorder_destroy(recorder_id);
                check_status(status, "pjsua_recorder_destroy")?;
            }
            info!("Stopped recording call {}", call_id);
            Ok(())
        } else {
            Err(EndpointError::Other("no active recording for this call".into()))
        }
    }

    /// Start asynchronous beep detection on a call.
    ///
    /// Runs in the background on incoming audio frames. Fires either
    /// `EndpointEvent::BeepDetected` or `EndpointEvent::BeepTimeout`.
    /// Only one detection can be active per call.
    pub fn detect_beep(&self, call_id: i32, config: BeepDetectorConfig) -> Result<()> {
        let mut state = global_state().lock().unwrap();
        // One detector per call — replace any existing one
        if state.beep_detectors.contains_key(&call_id) {
            debug!("Replacing existing beep detector on call {}", call_id);
        }
        let detector = BeepDetector::new(config);
        state.beep_detectors.insert(call_id, detector);
        info!("Beep detection started on call {}", call_id);
        Ok(())
    }

    /// Cancel beep detection on a call.
    pub fn cancel_beep_detection(&self, call_id: i32) -> Result<()> {
        let mut state = global_state().lock().unwrap();
        if state.beep_detectors.remove(&call_id).is_some() {
            info!("Beep detection cancelled on call {}", call_id);
            Ok(())
        } else {
            Err(EndpointError::Other("no beep detection active on this call".into()))
        }
    }

    /// Get the event receiver channel.
    pub fn events(&self) -> Receiver<EndpointEvent> {
        self.event_rx.clone()
    }

    /// Shut down the endpoint. Hangs up all calls and tears down the SIP stack.
    pub fn shutdown(&self) -> Result<()> {
        if !self.initialized.swap(false, Ordering::Relaxed) {
            return Ok(());
        }

        unsafe {
            pjsua_call_hangup_all();
            pjsua_destroy();
        }

        info!("Agent endpoint shut down");
        Ok(())
    }
}

impl Drop for SipEndpoint {
    fn drop(&mut self) {
        let _ = self.shutdown();
    }
}

// ─── SIP Callbacks ──────────────────────────────────────────────────────────

unsafe extern "C" fn on_reg_state(acc_id: pjsua_acc_id, info: *mut pjsua_reg_info) {
    let state = match global_state().lock() {
        Ok(s) => s,
        Err(_) => return,
    };

    if info.is_null() {
        return;
    }

    let cbparam = (*info).cbparam;
    if cbparam.is_null() {
        return;
    }

    let status = (*cbparam).status;
    let code = (*cbparam).code;

    let event = if status == pj_constants__PJ_SUCCESS as i32 && (code / 100 == 2) {
        info!("Registration successful (acc_id={})", acc_id);
        EndpointEvent::Registered
    } else if code == 0 || code == 408 {
        warn!("Unregistered (acc_id={})", acc_id);
        EndpointEvent::Unregistered
    } else {
        error!("Registration failed (acc_id={}, code={})", acc_id, code);
        EndpointEvent::RegistrationFailed {
            error: format!("SIP {}", code),
        }
    };

    let _ = state.event_tx.try_send(event);
}

unsafe extern "C" fn on_incoming_call(
    _acc_id: pjsua_acc_id,
    call_id: pjsua_call_id,
    _rdata: *mut pjsip_rx_data,
) {
    let mut state = match global_state().lock() {
        Ok(s) => s,
        Err(_) => return,
    };

    let mut session = CallSession::new(call_id, CallDirection::Inbound);

    // Get call info for remote URI
    if let Ok(ci) = get_call_info(call_id) {
        session.remote_uri = pj_str_to_string(&ci.remote_info);
        session.local_uri = pj_str_to_string(&ci.local_info);
    }

    info!(
        "Incoming call from {} (call_id={})",
        session.remote_uri, call_id
    );

    state.calls.insert(call_id, session.clone());

    let _ = state.event_tx.try_send(EndpointEvent::IncomingCall { session });
}

unsafe extern "C" fn on_call_state(call_id: pjsua_call_id, _e: *mut pjsip_event) {
    let ci = match get_call_info(call_id) {
        Ok(ci) => ci,
        Err(_) => return,
    };

    let mut state = match global_state().lock() {
        Ok(s) => s,
        Err(_) => return,
    };

    let call_state = match ci.state {
        val if val == pjsip_inv_state_PJSIP_INV_STATE_CALLING => CallState::Calling,
        val if val == pjsip_inv_state_PJSIP_INV_STATE_INCOMING => CallState::Incoming,
        val if val == pjsip_inv_state_PJSIP_INV_STATE_EARLY => CallState::Early,
        val if val == pjsip_inv_state_PJSIP_INV_STATE_CONNECTING => CallState::Connecting,
        val if val == pjsip_inv_state_PJSIP_INV_STATE_CONFIRMED => CallState::Confirmed,
        val if val == pjsip_inv_state_PJSIP_INV_STATE_DISCONNECTED => CallState::Disconnected,
        _ => CallState::Failed("unknown state".into()),
    };

    debug!("Call {} state: {:?}", call_id, call_state);

    // Update session
    if let Some(session) = state.calls.get_mut(&call_id) {
        session.state = call_state.clone();
        session.remote_uri = pj_str_to_string(&ci.remote_info);
        session.local_uri = pj_str_to_string(&ci.local_info);

        let session_clone = session.clone();

        if matches!(call_state, CallState::Disconnected | CallState::Failed(_)) {
            // Clean up
            let reason = pj_str_to_string(&ci.last_status_text);
            state.calls.remove(&call_id);
            if let Some(port) = state.media_ports.remove(&call_id) {
                port.destroy();
            }
            if let Some(rec_id) = state.recorders.remove(&call_id) {
                pjsua_recorder_destroy(rec_id);
            }
            state.beep_detectors.remove(&call_id);

            let _ = state.event_tx.try_send(EndpointEvent::CallTerminated {
                session: session_clone,
                reason,
            });
        } else {
            let _ = state.event_tx.try_send(EndpointEvent::CallStateChanged {
                session: session_clone,
            });
        }
    }
}

unsafe extern "C" fn on_call_media_state(call_id: pjsua_call_id) {
    let ci = match get_call_info(call_id) {
        Ok(ci) => ci,
        Err(_) => return,
    };

    if ci.media_status == pjsua_call_media_status_PJSUA_CALL_MEDIA_ACTIVE {
        let clock_rate;
        let event_tx;
        {
            let mut state = global_state().lock().unwrap();
            clock_rate = state.clock_rate;
            event_tx = state.event_tx.clone();
            // Destroy existing media port for this call (ICE renegotiation can
            // trigger on_call_media_state multiple times)
            if let Some(old_port) = state.media_ports.remove(&call_id) {
                debug!("Replacing existing media port for call {}", call_id);
                old_port.destroy();
            }
        }

        let channels = 1u32;
        let samples_per_frame = clock_rate / 50; // 20ms frames

        match CustomMediaPort::new(call_id, clock_rate, channels, samples_per_frame, event_tx) {
            Ok(port) => {
                if let Err(e) = port.connect_to_call(ci.conf_slot as i32) {
                    error!("Failed to connect media port to call {}: {}", call_id, e);
                    return;
                }

                info!(
                    "Call {} media active (conf_slot={}, port={})",
                    call_id, ci.conf_slot, port.conf_slot
                );

                if let Ok(mut state) = global_state().lock() {
                    state.media_ports.insert(call_id, port);
                    let _ = state
                        .event_tx
                        .try_send(EndpointEvent::CallMediaActive { call_id });
                }
            }
            Err(status) => {
                error!("Failed to create media port for call {}: {}", call_id, status);
            }
        }
    }
}

unsafe extern "C" fn on_dtmf_digit2(call_id: pjsua_call_id, info: *const pjsua_dtmf_info) {
    if info.is_null() {
        return;
    }
    let dtmf_info = &*info;
    let ch = char::from(dtmf_info.digit as u8);
    let method = match dtmf_info.method {
        pjsua_dtmf_method_PJSUA_DTMF_METHOD_SIP_INFO => "sip_info",
        _ => "rfc2833",
    };
    debug!("DTMF received on call {} ({}): {}", call_id, method, ch);

    if let Ok(state) = global_state().lock() {
        let _ = state.event_tx.try_send(EndpointEvent::DtmfReceived {
            call_id,
            digit: ch,
            method: method.to_string(),
        });
    }
}

unsafe extern "C" fn on_call_transfer_status(
    call_id: pjsua_call_id,
    st_code: ::std::os::raw::c_int,
    st_text: *const pj_str_t,
    final_: pj_bool_t,
    p_cont: *mut pj_bool_t,
) {
    let text = if !st_text.is_null() {
        pj_str_to_string(&*st_text)
    } else {
        String::new()
    };

    info!(
        "Transfer status for call {}: {} {} (final={})",
        call_id, st_code, text, final_
    );

    // Continue monitoring transfer status
    if !p_cont.is_null() {
        *p_cont = pj_constants__PJ_TRUE as i32;
    }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

unsafe fn get_call_info(call_id: pjsua_call_id) -> Result<pjsua_call_info> {
    let mut ci: pjsua_call_info = std::mem::zeroed();
    let status = pjsua_call_get_info(call_id, &mut ci);
    if status != pj_constants__PJ_SUCCESS as i32 {
        return Err(EndpointError::InvalidCallId(call_id));
    }
    Ok(ci)
}

unsafe fn pj_str_from_cstr(s: &CString) -> pj_str_t {
    pj_str_t {
        ptr: s.as_ptr() as *mut _,
        slen: s.as_bytes().len() as _,
    }
}

unsafe fn pj_str_to_string(s: &pj_str_t) -> String {
    if s.ptr.is_null() || s.slen <= 0 {
        return String::new();
    }
    let slice = std::slice::from_raw_parts(s.ptr as *const u8, s.slen as usize);
    String::from_utf8_lossy(slice).into_owned()
}

fn check_status(status: i32, context: &str) -> Result<()> {
    if status == pj_constants__PJ_SUCCESS as i32 {
        Ok(())
    } else {
        error!("{} failed with status {}", context, status);
        Err(EndpointError::Pjsua {
            code: status,
            message: format!("{} failed", context),
        })
    }
}
