//! SDP offer/answer construction and STUN binding for public IP discovery.

use std::net::{IpAddr, Ipv4Addr, SocketAddr, ToSocketAddrs, UdpSocket};
use std::time::{SystemTime, UNIX_EPOCH};

use tracing::{debug, warn};

use crate::config::Codec;
use crate::error::{EndpointError, Result};
use crate::rtp_transport::DTMF_PAYLOAD_TYPE;

/// Parsed SDP answer from remote party.
#[derive(Debug, Clone)]
pub(crate) struct SdpAnswer {
    pub remote_ip: IpAddr,
    pub remote_port: u16,
    pub codec: Codec,
    pub payload_type: u8,
    pub dtmf_payload_type: Option<u8>,
}

/// Build an SDP offer for an audio-only call.
pub(crate) fn build_offer(local_ip: IpAddr, rtp_port: u16, codecs: &[Codec]) -> String {
    let session_id = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();

    // Build payload type list (e.g., "0 8 101")
    let pt_list: Vec<String> = codecs
        .iter()
        .map(|c| c.payload_type().to_string())
        .chain(std::iter::once(DTMF_PAYLOAD_TYPE.to_string()))
        .collect();

    let mut sdp = format!(
        "v=0\r\n\
         o=- {} 1 IN IP4 {}\r\n\
         s=-\r\n\
         c=IN IP4 {}\r\n\
         t=0 0\r\n\
         m=audio {} RTP/AVP {}\r\n",
        session_id,
        local_ip,
        local_ip,
        rtp_port,
        pt_list.join(" "),
    );

    // Add rtpmap for each codec
    for codec in codecs {
        sdp.push_str(&format!(
            "a=rtpmap:{} {}\r\n",
            codec.payload_type(),
            codec.rtpmap_line(),
        ));
    }

    // DTMF telephone-event
    sdp.push_str(&format!(
        "a=rtpmap:{} telephone-event/8000\r\n\
         a=fmtp:{} 0-16\r\n",
        DTMF_PAYLOAD_TYPE, DTMF_PAYLOAD_TYPE,
    ));

    sdp.push_str("a=ptime:20\r\na=sendrecv\r\n");
    sdp
}

/// Parse an SDP answer to extract remote RTP address and chosen codec.
pub(crate) fn parse_answer(sdp_bytes: &[u8], offered_codecs: &[Codec]) -> Result<SdpAnswer> {
    let sdp_str = std::str::from_utf8(sdp_bytes)
        .map_err(|_| EndpointError::Other("invalid UTF-8 in SDP".into()))?;

    let mut remote_ip: Option<IpAddr> = None;
    let mut remote_port: Option<u16> = None;
    let mut accepted_pts: Vec<u8> = Vec::new();
    let mut dtmf_pt: Option<u8> = None;

    for line in sdp_str.lines() {
        let line = line.trim();

        // Connection line: c=IN IP4 1.2.3.4
        if line.starts_with("c=IN IP4 ") {
            if let Ok(ip) = line[9..].trim().parse::<IpAddr>() {
                remote_ip = Some(ip);
            }
        } else if line.starts_with("c=IN IP6 ") {
            if let Ok(ip) = line[9..].trim().parse::<IpAddr>() {
                remote_ip = Some(ip);
            }
        }

        // Media line: m=audio 12345 RTP/AVP 0 8 101
        if line.starts_with("m=audio ") {
            let parts: Vec<&str> = line.split_whitespace().collect();
            if parts.len() >= 4 {
                remote_port = parts[1].parse().ok();
                // Payload types are parts[3..]
                accepted_pts = parts[3..].iter().filter_map(|p| p.parse().ok()).collect();
            }
        }

        // Detect telephone-event PT from rtpmap
        if line.starts_with("a=rtpmap:") {
            let rest = &line[9..];
            if rest.contains("telephone-event") {
                if let Some(pt_str) = rest.split_whitespace().next() {
                    dtmf_pt = pt_str.parse().ok();
                }
            }
        }
    }

    let remote_ip =
        remote_ip.ok_or_else(|| EndpointError::Other("no connection line in SDP".into()))?;
    let remote_port =
        remote_port.ok_or_else(|| EndpointError::Other("no media line in SDP".into()))?;

    // Find the first accepted codec that we offered
    let (codec, pt) = offered_codecs
        .iter()
        .find_map(|c| {
            let pt = c.payload_type();
            if accepted_pts.contains(&pt) {
                Some((*c, pt))
            } else {
                None
            }
        })
        .ok_or_else(|| EndpointError::Other("no common codec in SDP answer".into()))?;

    Ok(SdpAnswer {
        remote_ip,
        remote_port,
        codec,
        payload_type: pt,
        dtmf_payload_type: dtmf_pt,
    })
}

/// Discover our public IP via a STUN Binding Request (RFC 5389).
///
/// Sends a minimal 20-byte STUN request and parses the XOR-MAPPED-ADDRESS
/// from the response. Uses a blocking UDP socket with a short timeout.
pub(crate) fn stun_binding(stun_server: &str) -> Result<SocketAddr> {
    let server_addr = stun_server
        .to_socket_addrs()
        .map_err(|e| EndpointError::Other(format!("STUN server resolve: {}", e)))?
        .next()
        .ok_or_else(|| EndpointError::Other("STUN server address not found".into()))?;

    let socket = UdpSocket::bind("0.0.0.0:0")
        .map_err(|e| EndpointError::Other(format!("STUN bind: {}", e)))?;
    socket
        .set_read_timeout(Some(std::time::Duration::from_secs(3)))
        .ok();

    // STUN Binding Request: 20 bytes
    // Type: 0x0001 (Binding Request)
    // Length: 0x0000 (no attributes)
    // Magic cookie: 0x2112A442
    // Transaction ID: 12 random bytes
    let mut request = [0u8; 20];
    request[0] = 0x00;
    request[1] = 0x01; // Binding Request
    // Length = 0 (bytes 2-3)
    request[4] = 0x21;
    request[5] = 0x12;
    request[6] = 0xA4;
    request[7] = 0x42; // Magic cookie
    let txn_id: [u8; 12] = rand::random();
    request[8..20].copy_from_slice(&txn_id);

    socket
        .send_to(&request, server_addr)
        .map_err(|e| EndpointError::Other(format!("STUN send: {}", e)))?;

    let mut response = [0u8; 512];
    let len = socket
        .recv(&mut response)
        .map_err(|e| EndpointError::Other(format!("STUN recv: {}", e)))?;

    if len < 20 {
        return Err(EndpointError::Other("STUN response too short".into()));
    }

    // Parse attributes looking for XOR-MAPPED-ADDRESS (0x0020)
    // or MAPPED-ADDRESS (0x0001) as fallback
    let msg_len = u16::from_be_bytes([response[2], response[3]]) as usize;
    let mut pos = 20;
    let end = (20 + msg_len).min(len);

    while pos + 4 <= end {
        let attr_type = u16::from_be_bytes([response[pos], response[pos + 1]]);
        let attr_len = u16::from_be_bytes([response[pos + 2], response[pos + 3]]) as usize;
        let attr_start = pos + 4;

        if attr_type == 0x0020 && attr_len >= 8 {
            // XOR-MAPPED-ADDRESS
            let family = response[attr_start + 1];
            if family == 0x01 {
                // IPv4
                let xor_port =
                    u16::from_be_bytes([response[attr_start + 2], response[attr_start + 3]])
                        ^ 0x2112;
                let xor_ip = u32::from_be_bytes([
                    response[attr_start + 4],
                    response[attr_start + 5],
                    response[attr_start + 6],
                    response[attr_start + 7],
                ]) ^ 0x2112A442;
                let ip = Ipv4Addr::from(xor_ip);
                debug!("STUN discovered public address: {}:{}", ip, xor_port);
                return Ok(SocketAddr::new(IpAddr::V4(ip), xor_port));
            }
        } else if attr_type == 0x0001 && attr_len >= 8 {
            // MAPPED-ADDRESS (fallback)
            let family = response[attr_start + 1];
            if family == 0x01 {
                let port =
                    u16::from_be_bytes([response[attr_start + 2], response[attr_start + 3]]);
                let ip = Ipv4Addr::new(
                    response[attr_start + 4],
                    response[attr_start + 5],
                    response[attr_start + 6],
                    response[attr_start + 7],
                );
                debug!("STUN discovered public address (MAPPED): {}:{}", ip, port);
                return Ok(SocketAddr::new(IpAddr::V4(ip), port));
            }
        }

        // Advance to next attribute (padded to 4-byte boundary)
        pos = attr_start + ((attr_len + 3) & !3);
    }

    // Fallback: use local IP
    warn!("STUN: no mapped address in response, using local IP");
    let local_addr = socket.local_addr().map_err(|e| {
        EndpointError::Other(format!("local addr: {}", e))
    })?;
    Ok(local_addr)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_build_offer_pcmu() {
        let sdp = build_offer(
            IpAddr::V4(Ipv4Addr::new(192, 168, 1, 100)),
            10000,
            &[Codec::PCMU],
        );
        assert!(sdp.contains("m=audio 10000 RTP/AVP 0 101"));
        assert!(sdp.contains("a=rtpmap:0 PCMU/8000"));
        assert!(sdp.contains("a=rtpmap:101 telephone-event/8000"));
        assert!(sdp.contains("a=sendrecv"));
        assert!(sdp.contains("c=IN IP4 192.168.1.100"));
    }

    #[test]
    fn test_build_offer_multiple_codecs() {
        let sdp = build_offer(
            IpAddr::V4(Ipv4Addr::new(10, 0, 0, 1)),
            20000,
            &[Codec::PCMU, Codec::PCMA],
        );
        assert!(sdp.contains("m=audio 20000 RTP/AVP 0 8 101"));
        assert!(sdp.contains("a=rtpmap:0 PCMU/8000"));
        assert!(sdp.contains("a=rtpmap:8 PCMA/8000"));
    }

    #[test]
    fn test_parse_answer_basic() {
        let sdp = b"v=0\r\n\
            o=- 123 1 IN IP4 203.0.113.5\r\n\
            s=-\r\n\
            c=IN IP4 203.0.113.5\r\n\
            t=0 0\r\n\
            m=audio 30000 RTP/AVP 0 101\r\n\
            a=rtpmap:0 PCMU/8000\r\n\
            a=rtpmap:101 telephone-event/8000\r\n\
            a=fmtp:101 0-16\r\n\
            a=sendrecv\r\n";

        let answer = parse_answer(sdp, &[Codec::PCMU, Codec::PCMA]).unwrap();
        assert_eq!(
            answer.remote_ip,
            IpAddr::V4(Ipv4Addr::new(203, 0, 113, 5))
        );
        assert_eq!(answer.remote_port, 30000);
        assert_eq!(answer.codec, Codec::PCMU);
        assert_eq!(answer.payload_type, 0);
        assert_eq!(answer.dtmf_payload_type, Some(101));
    }

    #[test]
    fn test_parse_answer_no_connection() {
        let sdp = b"v=0\r\nm=audio 5000 RTP/AVP 0\r\n";
        let result = parse_answer(sdp, &[Codec::PCMU]);
        assert!(result.is_err());
    }

    #[test]
    fn test_parse_answer_no_common_codec() {
        let sdp = b"v=0\r\nc=IN IP4 1.2.3.4\r\nm=audio 5000 RTP/AVP 8\r\n";
        // We only offer PCMU (0) but answer has only PCMA (8)
        let result = parse_answer(sdp, &[Codec::PCMU]);
        assert!(result.is_err());
    }
}
