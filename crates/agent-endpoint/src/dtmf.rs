use std::ffi::CString;

use tracing::info;

use crate::pj_helpers::pj_str_from_cstr;
use crate::error::{EndpointError, Result};

use pjsua_sys::*;

/// Send DTMF digits on a call using the specified method.
///
/// Methods: "rfc2833" (RTP events), "sip_info" (SIP INFO message)
pub(crate) fn send_dtmf(call_id: i32, digits: &str, method: &str) -> Result<()> {
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
        if status != pj_constants__PJ_SUCCESS as i32 {
            return Err(EndpointError::Pjsua {
                code: status,
                message: "pjsua_call_send_dtmf failed".into(),
            });
        }
    }
    info!("DTMF sent on call {} ({}): {}", call_id, method, digits);
    Ok(())
}
