//! SIP callback functions registered with the pjsua stack.
//!
//! These are `unsafe extern "C"` functions invoked by the C SIP library
//! on registration state changes, incoming calls, call state transitions,
//! media events, DTMF digits, and transfer status.

use std::ffi::CString;

use tracing::{debug, error, info, warn};

use crate::call::{CallDirection, CallSession, CallState};
use crate::endpoint::global_state;
use crate::events::EndpointEvent;
use crate::media_port::CustomMediaPort;
use crate::pj_helpers::{
    extract_custom_headers, extract_headers_from_event, get_call_info, pj_str_to_string,
};

use pjsua_sys::*;

pub(crate) unsafe extern "C" fn on_reg_state(
    acc_id: pjsua_acc_id,
    info: *mut pjsua_reg_info,
) {
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

pub(crate) unsafe extern "C" fn on_incoming_call(
    _acc_id: pjsua_acc_id,
    call_id: pjsua_call_id,
    rdata: *mut pjsip_rx_data,
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

    // Extract all X-* custom headers from the incoming INVITE
    if !rdata.is_null() {
        let msg = (*rdata).msg_info.msg;
        session.extra_headers = extract_custom_headers(msg);

        // Set call_uuid from known header names
        if let Some(uuid) = session.extra_headers.get("X-CallUUID") {
            session.call_uuid = Some(uuid.clone());
        } else if let Some(uuid) = session.extra_headers.get("X-Plivo-CallUUID") {
            session.call_uuid = Some(uuid.clone());
        }
    }

    info!(
        "Incoming call from {} (call_id={}, uuid={:?})",
        session.remote_uri, call_id, session.call_uuid
    );

    state.calls.insert(call_id, session.clone());

    let _ = state.event_tx.try_send(EndpointEvent::IncomingCall { session });
}

pub(crate) unsafe extern "C" fn on_call_state(
    call_id: pjsua_call_id,
    e: *mut pjsip_event,
) {
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
        val if val == pjsip_inv_state_PJSIP_INV_STATE_DISCONNECTED => {
            CallState::Disconnected
        }
        _ => CallState::Failed("unknown state".into()),
    };

    debug!("Call {} state: {:?}", call_id, call_state);

    // Extract X-* headers from the SIP message that triggered this state change
    // (e.g. 180 Ringing, 200 OK, BYE). Merge into session.extra_headers.
    let new_headers = extract_headers_from_event(e);

    // Update session
    if let Some(session) = state.calls.get_mut(&call_id) {
        session.state = call_state.clone();
        session.remote_uri = pj_str_to_string(&ci.remote_info);
        session.local_uri = pj_str_to_string(&ci.local_info);

        // Merge newly received headers (later messages override earlier ones)
        for (k, v) in &new_headers {
            session.extra_headers.insert(k.clone(), v.clone());
        }

        // Update call_uuid if we got it from a response (outbound calls
        // won't have it until the remote side responds)
        if session.call_uuid.is_none() {
            if let Some(uuid) = session.extra_headers.get("X-CallUUID") {
                session.call_uuid = Some(uuid.clone());
            } else if let Some(uuid) = session.extra_headers.get("X-Plivo-CallUUID") {
                session.call_uuid = Some(uuid.clone());
            }
        }

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

pub(crate) unsafe extern "C" fn on_call_media_state(call_id: pjsua_call_id) {
    let ci = match get_call_info(call_id) {
        Ok(ci) => ci,
        Err(_) => return,
    };

    if ci.media_status == pjsua_call_media_status_PJSUA_CALL_MEDIA_ACTIVE {
        let clock_rate;
        let event_tx;
        let use_sound_device;
        {
            let mut state = global_state().lock().unwrap();
            clock_rate = state.clock_rate;
            event_tx = state.event_tx.clone();
            use_sound_device = state.use_sound_device;
            // Destroy existing media port for this call (ICE renegotiation can
            // trigger on_call_media_state multiple times)
            if let Some(old_port) = state.media_ports.remove(&call_id) {
                debug!("Replacing existing media port for call {}", call_id);
                old_port.destroy();
            }
        }

        // Connect call to system sound device (mic + speaker) if enabled.
        // Slot 0 is always the sound device in pjsua's conference bridge.
        if use_sound_device {
            let call_slot = ci.conf_slot as i32;
            pjsua_conf_connect(call_slot, 0); // call → speaker
            pjsua_conf_connect(0, call_slot); // mic → call
            info!("Call {} connected to sound device (conf_slot={})", call_id, call_slot);
        }

        let channels = 1u32;
        let samples_per_frame = clock_rate / 50; // 20ms frames

        // Also create the programmatic media port (for send_audio/recv_audio/beep detection)
        match CustomMediaPort::new(call_id, clock_rate, channels, samples_per_frame, event_tx)
        {
            Ok(port) => {
                if let Err(e) = port.connect_to_call(ci.conf_slot as i32) {
                    error!(
                        "Failed to connect media port to call {}: {}",
                        call_id, e
                    );
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
                error!(
                    "Failed to create media port for call {}: {}",
                    call_id, status
                );
            }
        }
    }
}

pub(crate) unsafe extern "C" fn on_dtmf_digit2(
    call_id: pjsua_call_id,
    info: *const pjsua_dtmf_info,
) {
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

pub(crate) unsafe extern "C" fn on_call_transfer_status(
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
