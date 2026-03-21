//! DTMF send/receive — RFC 4733 (RTP telephone-event) and SIP INFO.

/// Map a DTMF digit character to RFC 4733 event code.
pub(crate) fn digit_to_event(digit: char) -> Option<u8> {
    match digit {
        '0' => Some(0),
        '1' => Some(1),
        '2' => Some(2),
        '3' => Some(3),
        '4' => Some(4),
        '5' => Some(5),
        '6' => Some(6),
        '7' => Some(7),
        '8' => Some(8),
        '9' => Some(9),
        '*' => Some(10),
        '#' => Some(11),
        'A' | 'a' => Some(12),
        'B' | 'b' => Some(13),
        'C' | 'c' => Some(14),
        'D' | 'd' => Some(15),
        _ => None,
    }
}

/// Map an RFC 4733 event code back to a digit character.
pub(crate) fn event_to_digit(event: u8) -> Option<char> {
    match event {
        0 => Some('0'),
        1 => Some('1'),
        2 => Some('2'),
        3 => Some('3'),
        4 => Some('4'),
        5 => Some('5'),
        6 => Some('6'),
        7 => Some('7'),
        8 => Some('8'),
        9 => Some('9'),
        10 => Some('*'),
        11 => Some('#'),
        12 => Some('A'),
        13 => Some('B'),
        14 => Some('C'),
        15 => Some('D'),
        _ => None,
    }
}

/// Encode an RFC 4733 DTMF event payload (4 bytes).
///
/// Layout:
/// ```text
///  0                   1                   2                   3
///  0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
/// +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
/// |     event     |E|R| volume    |          duration             |
/// +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
/// ```
pub(crate) fn encode_rfc4733(event: u8, end: bool, volume: u8, duration: u16) -> [u8; 4] {
    let mut payload = [0u8; 4];
    payload[0] = event;
    payload[1] = if end { 0x80 } else { 0x00 } | (volume & 0x3F);
    payload[2] = (duration >> 8) as u8;
    payload[3] = duration as u8;
    payload
}

/// Decode an RFC 4733 DTMF event payload.
/// Returns (event_code, is_end, volume, duration).
pub(crate) fn decode_rfc4733(payload: &[u8]) -> Option<(u8, bool, u8, u16)> {
    if payload.len() < 4 {
        return None;
    }
    let event = payload[0];
    let end = (payload[1] & 0x80) != 0;
    let volume = payload[1] & 0x3F;
    let duration = u16::from_be_bytes([payload[2], payload[3]]);
    Some((event, end, volume, duration))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_digit_event_roundtrip() {
        for digit in "0123456789*#ABCD".chars() {
            let event = digit_to_event(digit).unwrap();
            let back = event_to_digit(event).unwrap();
            assert_eq!(back, digit.to_ascii_uppercase());
        }
    }

    #[test]
    fn test_rfc4733_encode_decode() {
        let payload = encode_rfc4733(5, false, 10, 1600);
        let (event, end, volume, duration) = decode_rfc4733(&payload).unwrap();
        assert_eq!(event, 5);
        assert!(!end);
        assert_eq!(volume, 10);
        assert_eq!(duration, 1600);
    }

    #[test]
    fn test_rfc4733_end_flag() {
        let payload = encode_rfc4733(11, true, 10, 3200);
        let (event, end, _, duration) = decode_rfc4733(&payload).unwrap();
        assert_eq!(event, 11); // '#'
        assert!(end);
        assert_eq!(duration, 3200);
    }
}
