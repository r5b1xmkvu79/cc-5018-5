"""RTP (RFC 3550) packet handling for MPEG-TS payload (PT=33)."""
import struct

RTP_PT_MP2T = 33
SEQ_SPACE = 1 << 16
HALF_SPACE = 1 << 15
TS_CLOCK_HZ = 90000
TS_PACKET_SIZE = 188
RTP_MTU = 7 * TS_PACKET_SIZE  # 1316, standard rtp_mpegts payload size


class RtpParseError(ValueError):
    pass


def seq_delta(seq, base):
    """Signed forward distance from base to seq on the 16-bit circular space."""
    d = (seq - base) & 0xFFFF
    if d >= HALF_SPACE:
        d -= SEQ_SPACE
    return d


def parse_rtp(data):
    if len(data) < 12:
        raise RtpParseError("packet shorter than RTP header")
    b0, b1, seq, rtp_ts, ssrc = struct.unpack("!BBHII", data[:12])
    version = b0 >> 6
    if version != 2:
        raise RtpParseError("not RTPv2")
    csrc_count = b0 & 0x0F
    extension = bool(b0 & 0x10)
    payload_type = b1 & 0x7F
    marker = bool(b1 & 0x80)
    offset = 12 + 4 * csrc_count
    if extension:
        if len(data) < offset + 4:
            raise RtpParseError("truncated extension header")
        ext_words = struct.unpack("!H", data[offset + 2:offset + 4])[0]
        offset += 4 + 4 * ext_words
    return {
        "seq": seq,
        "ts": rtp_ts,
        "ssrc": ssrc,
        "pt": payload_type,
        "marker": marker,
        "payload": data[offset:],
    }


def build_rtp(seq, rtp_ts, ssrc, payload, marker=False, pt=RTP_PT_MP2T):
    b0 = 0x80  # V=2
    b1 = (0x80 if marker else 0) | (pt & 0x7F)
    header = struct.pack("!BBHII", b0, b1, seq & 0xFFFF, rtp_ts & 0xFFFFFFFF, ssrc)
    return header + payload


def split_ts_packets(payload):
    """Yield 188-byte TS packets; ignore a trailing partial chunk."""
    n = len(payload) // TS_PACKET_SIZE
    for i in range(n):
        yield payload[i * TS_PACKET_SIZE:(i + 1) * TS_PACKET_SIZE]
