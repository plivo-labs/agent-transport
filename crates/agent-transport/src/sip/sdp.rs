//! SDP offer/answer construction and STUN binding for public IP discovery.

use std::net::{IpAddr, Ipv4Addr, SocketAddr, ToSocketAddrs, UdpSocket};
use std::time::{SystemTime, UNIX_EPOCH};

use tracing::{debug, warn};

use crate::config::Codec;
use crate::error::{EndpointError, Result};
use crate::sip::rtp_transport::DEFAULT_DTMF_PT;

#[derive(Debug, Clone)]
pub(crate) struct SdpAnswer {
    pub remote_ip: IpAddr,
    pub remote_port: u16,
    pub codec: Codec,
    pub payload_type: u8,
    pub dtmf_payload_type: Option<u8>,
    pub ptime_ms: u32, // #10: parsed from remote SDP
}

pub(crate) fn build_offer(local_ip: IpAddr, rtp_port: u16, codecs: &[Codec]) -> String {
    let sid = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
    let pts: Vec<String> = codecs.iter().map(|c| c.payload_type().to_string())
        .chain(std::iter::once(DEFAULT_DTMF_PT.to_string())).collect();

    let mut sdp = format!(
        "v=0\r\no=- {} 1 IN IP4 {}\r\ns=-\r\nc=IN IP4 {}\r\nt=0 0\r\nm=audio {} RTP/AVP {}\r\n",
        sid, local_ip, local_ip, rtp_port, pts.join(" "),
    );
    for c in codecs {
        sdp.push_str(&format!("a=rtpmap:{} {}\r\n", c.payload_type(), c.rtpmap_line()));
    }
    // #1: Fix fmtp — 0-15 not 0-16 (16 DTMF events: digits 0-9, *, #, A-D = 0-15)
    sdp.push_str(&format!("a=rtpmap:{} telephone-event/8000\r\na=fmtp:{} 0-15\r\na=ptime:20\r\na=sendrecv\r\n", DEFAULT_DTMF_PT, DEFAULT_DTMF_PT));
    sdp
}

pub(crate) fn parse_answer(sdp_bytes: &[u8], offered_codecs: &[Codec]) -> Result<SdpAnswer> {
    let sdp = std::str::from_utf8(sdp_bytes).map_err(|_| EndpointError::Other("invalid UTF-8 in SDP".into()))?;

    // #23: Validate mandatory SDP fields
    let has_v = sdp.lines().any(|l| l.trim().starts_with("v="));
    if !has_v { return Err(EndpointError::Other("SDP missing v= line".into())); }

    let mut remote_ip: Option<IpAddr> = None;
    let mut remote_port: Option<u16> = None;
    let mut accepted_pts: Vec<u8> = Vec::new();
    let mut dtmf_pt: Option<u8> = None;
    let mut ptime_ms: u32 = 20; // default

    for line in sdp.lines() {
        let line = line.trim();
        if let Some(rest) = line.strip_prefix("c=IN IP4 ") { remote_ip = rest.trim().parse().ok(); }
        else if let Some(rest) = line.strip_prefix("c=IN IP6 ") { remote_ip = rest.trim().parse().ok(); }
        else if line.starts_with("m=audio ") {
            let p: Vec<&str> = line.split_whitespace().collect();
            if p.len() >= 4 {
                // #13: Port range validation
                let port_val: u32 = p[1].parse().unwrap_or(0);
                if port_val > 65535 {
                    return Err(EndpointError::Other(format!("SDP port out of range: {}", port_val)));
                }
                remote_port = Some(port_val as u16);
                accepted_pts = p[3..].iter().filter_map(|x| x.parse().ok()).collect();
            }
        } else if let Some(rest) = line.strip_prefix("a=rtpmap:") {
            if rest.contains("telephone-event") { dtmf_pt = rest.split_whitespace().next().and_then(|s| s.parse().ok()); }
        } else if let Some(rest) = line.strip_prefix("a=ptime:") {
            // #10: Parse ptime from remote SDP
            ptime_ms = rest.trim().parse().unwrap_or(20);
        }
    }

    let remote_ip = remote_ip.ok_or_else(|| EndpointError::Other("no connection in SDP".into()))?;
    let remote_port = remote_port.ok_or_else(|| EndpointError::Other("no media in SDP".into()))?;

    // #2: Port 0 means stream rejected/hold (RFC 3264 §6)
    if remote_port == 0 {
        return Err(EndpointError::Other("SDP port 0: media stream rejected or on hold".into()));
    }

    let (codec, pt) = offered_codecs.iter().find_map(|c| { let pt = c.payload_type(); accepted_pts.contains(&pt).then_some((*c, pt)) })
        .ok_or_else(|| EndpointError::Other("no common codec".into()))?;

    Ok(SdpAnswer { remote_ip, remote_port, codec, payload_type: pt, dtmf_payload_type: dtmf_pt, ptime_ms })
}

/// STUN Binding Request (RFC 5389) — discover public IP.
pub(crate) fn stun_binding(stun_server: &str) -> Result<SocketAddr> {
    let addr = stun_server.to_socket_addrs().map_err(|e| EndpointError::Other(format!("STUN resolve: {}", e)))?
        .next().ok_or_else(|| EndpointError::Other("STUN: no address".into()))?;
    let sock = UdpSocket::bind("0.0.0.0:0").map_err(|e| EndpointError::Other(format!("STUN bind: {}", e)))?;
    sock.set_read_timeout(Some(std::time::Duration::from_secs(3))).ok();

    let mut req = [0u8; 20];
    req[0..2].copy_from_slice(&[0x00, 0x01]);
    req[4..8].copy_from_slice(&[0x21, 0x12, 0xA4, 0x42]);
    let txn: [u8; 12] = rand::random();
    req[8..20].copy_from_slice(&txn);

    sock.send_to(&req, addr).map_err(|e| EndpointError::Other(format!("STUN send: {}", e)))?;
    let mut resp = [0u8; 512];
    let len = sock.recv(&mut resp).map_err(|e| EndpointError::Other(format!("STUN recv: {}", e)))?;
    if len < 20 { return Err(EndpointError::Other("STUN response too short".into())); }

    let msg_len = u16::from_be_bytes([resp[2], resp[3]]) as usize;
    let mut pos = 20;
    let end = (20 + msg_len).min(len);
    while pos + 4 <= end {
        let at = u16::from_be_bytes([resp[pos], resp[pos + 1]]);
        let al = u16::from_be_bytes([resp[pos + 2], resp[pos + 3]]) as usize;
        let s = pos + 4;
        if at == 0x0020 && al >= 8 && resp[s + 1] == 0x01 {
            let port = u16::from_be_bytes([resp[s + 2], resp[s + 3]]) ^ 0x2112;
            let ip = u32::from_be_bytes([resp[s + 4], resp[s + 5], resp[s + 6], resp[s + 7]]) ^ 0x2112A442;
            debug!("STUN: public address {}:{}", Ipv4Addr::from(ip), port);
            return Ok(SocketAddr::new(IpAddr::V4(Ipv4Addr::from(ip)), port));
        }
        pos = s + ((al + 3) & !3);
    }
    warn!("STUN: no mapped address, using local");
    Ok(sock.local_addr().map_err(|e| EndpointError::Other(format!("local addr: {}", e)))?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_build_offer_fmtp_0_15() {
        let sdp = build_offer(IpAddr::V4(Ipv4Addr::new(192, 168, 1, 1)), 10000, &[Codec::PCMU]);
        assert!(sdp.contains("a=fmtp:101 0-15"), "fmtp should be 0-15 not 0-16: {}", sdp);
    }

    #[test]
    fn test_build_offer_pcmu() {
        let sdp = build_offer(IpAddr::V4(Ipv4Addr::new(192, 168, 1, 100)), 10000, &[Codec::PCMU]);
        assert!(sdp.contains("m=audio 10000 RTP/AVP 0 101"));
        assert!(sdp.contains("a=rtpmap:0 PCMU/8000"));
        assert!(sdp.contains("c=IN IP4 192.168.1.100"));
    }

    #[test]
    fn test_build_offer_multiple_codecs() {
        let sdp = build_offer(IpAddr::V4(Ipv4Addr::new(10, 0, 0, 1)), 20000, &[Codec::PCMU, Codec::PCMA]);
        assert!(sdp.contains("m=audio 20000 RTP/AVP 0 8 101"));
    }

    #[test]
    fn test_parse_answer_basic() {
        let sdp = b"v=0\r\nc=IN IP4 203.0.113.5\r\nm=audio 30000 RTP/AVP 0 101\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:101 telephone-event/8000\r\na=ptime:20\r\n";
        let a = parse_answer(sdp, &[Codec::PCMU, Codec::PCMA]).unwrap();
        assert_eq!(a.remote_ip, IpAddr::V4(Ipv4Addr::new(203, 0, 113, 5)));
        assert_eq!(a.remote_port, 30000);
        assert_eq!(a.codec, Codec::PCMU);
        assert_eq!(a.dtmf_payload_type, Some(101));
        assert_eq!(a.ptime_ms, 20);
    }

    #[test]
    fn test_parse_answer_port_0_rejected() {
        let sdp = b"v=0\r\nc=IN IP4 1.2.3.4\r\nm=audio 0 RTP/AVP 0\r\n";
        let result = parse_answer(sdp, &[Codec::PCMU]);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("port 0"));
    }

    #[test]
    fn test_parse_answer_no_connection() {
        assert!(parse_answer(b"v=0\r\nm=audio 5000 RTP/AVP 0\r\n", &[Codec::PCMU]).is_err());
    }

    #[test]
    fn test_parse_answer_no_common_codec() {
        assert!(parse_answer(b"v=0\r\nc=IN IP4 1.2.3.4\r\nm=audio 5000 RTP/AVP 8\r\n", &[Codec::PCMU]).is_err());
    }

    #[test]
    fn test_parse_answer_missing_v_line() {
        assert!(parse_answer(b"c=IN IP4 1.2.3.4\r\nm=audio 5000 RTP/AVP 0\r\n", &[Codec::PCMU]).is_err());
    }

    #[test]
    fn test_parse_answer_ptime() {
        let sdp = b"v=0\r\nc=IN IP4 1.2.3.4\r\nm=audio 5000 RTP/AVP 0\r\na=ptime:30\r\n";
        let a = parse_answer(sdp, &[Codec::PCMU]).unwrap();
        assert_eq!(a.ptime_ms, 30);
    }

    #[test]
    fn test_parse_answer_dynamic_dtmf_pt() {
        let sdp = b"v=0\r\nc=IN IP4 1.2.3.4\r\nm=audio 5000 RTP/AVP 0 96\r\na=rtpmap:96 telephone-event/8000\r\n";
        let a = parse_answer(sdp, &[Codec::PCMU]).unwrap();
        assert_eq!(a.dtmf_payload_type, Some(96)); // Not 101!
    }
}
