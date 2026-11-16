"""Transport demo asset format: recorded RTP wires, packet records, manifests.

A demo asset is a real RTP wire (not a description): each record holds the
packet's emission offset in microseconds, its original packet index and the
exact RTP datagram. The wire is produced from a generated MPEG-TS content
clip by packetizing at the transport MTU, then applying a declared fault plan
(drop / duplicate / delay) to the emission schedule. Everything downstream
(receiver replay, counters, evidence) consumes this file.
"""
import hashlib
import json
import struct
from pathlib import Path

from .. import rtp
from ..ts import TSAnalyzer

WIRE_MAGIC = b"TVRTPW1\n"
WIRE_HEADER = struct.Struct("<II")        # version, record count
WIRE_RECORD = struct.Struct("<QIH")       # offset_us, original index, length


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_wire(path, entries):
    """entries: iterable of (offset_us, original_index, rtp_bytes)."""
    entries = sorted(entries, key=lambda e: (e[0], e[1]))
    with open(path, "wb") as fh:
        fh.write(WIRE_MAGIC)
        fh.write(WIRE_HEADER.pack(1, len(entries)))
        for offset_us, index, packet in entries:
            fh.write(WIRE_RECORD.pack(offset_us, index, len(packet)))
            fh.write(packet)
    return entries


def read_wire(path):
    with open(path, "rb") as fh:
        if fh.read(len(WIRE_MAGIC)) != WIRE_MAGIC:
            raise ValueError(f"{path}: not a TVRTPW1 wire")
        version, count = WIRE_HEADER.unpack(fh.read(WIRE_HEADER.size))
        if version != 1:
            raise ValueError(f"{path}: unsupported wire version {version}")
        for _ in range(count):
            offset_us, index, length = WIRE_RECORD.unpack(fh.read(WIRE_RECORD.size))
            packet = fh.read(length)
            if len(packet) != length:
                raise ValueError(f"{path}: truncated wire record")
            yield offset_us, index, packet


def _payload_offset(pkt):
    afc = (pkt[3] >> 4) & 0x03
    if afc == 2:
        return None
    offset = 4
    if afc == 3:
        offset = 5 + pkt[4]
    return offset if offset < 188 else None


def _pes_pts(pkt, offset):
    b = pkt[offset:offset + 14]
    if len(b) < 14 or b[:3] != b"\x00\x00\x01":
        return None
    flags = b[7] >> 6
    if flags not in (2, 3):
        return None
    return (((b[9] >> 1) & 0x07) << 30 | b[10] << 22
            | (b[11] >> 1) << 15 | b[12] << 7 | (b[13] >> 1))


def index_content(ts_bytes):
    """Describe the generated TS clip: PID layout and video frame positions.

    Frame numbers are rendered into the picture; this index maps frame number
    -> PTS and first RTP packet index, so a manifest event can name both the
    packet identity and the picture that is affected.
    """
    packet_count = len(ts_bytes) // 188
    analyzer = TSAnalyzer()
    pids = []
    pcr_pid = None
    for i in range(packet_count):
        pkt = ts_bytes[i * 188:(i + 1) * 188]
        pids.append(((pkt[1] & 0x1F) << 8) | pkt[2])
        analyzer.feed(pkt, i * 0.001)

    snap = analyzer.snapshot()
    if not snap["pmt"]:
        raise ValueError("content TS has no PMT")
    pmt_pid = int(next(iter(snap["pmt"])))
    pmt = snap["pmt"][str(pmt_pid)]
    pcr_pid = pmt["pcr_pid"]
    video_pid = audio_pid = None
    for stream in pmt["streams"]:
        if stream["stream_type"] in (0x1B, 0x24, 0x01, 0x02) and video_pid is None:
            video_pid = stream["pid"]
        elif stream["stream_type"] in (0x0F, 0x03, 0x04, 0x11) and audio_pid is None:
            audio_pid = stream["pid"]
    if video_pid is None:
        raise ValueError("content TS has no video stream")

    frames = []
    for i in range(packet_count):
        if pids[i] != video_pid:
            continue
        pkt = ts_bytes[i * 188:(i + 1) * 188]
        if not (pkt[1] & 0x40):
            continue
        offset = _payload_offset(pkt)
        if offset is None:
            continue
        pts = _pes_pts(pkt, offset)
        frames.append({
            "index": len(frames),
            "pts": pts,
            "ts_packet_index": i,
        })

    return {
        "ts_packets": packet_count,
        "video_pid": video_pid,
        "audio_pid": audio_pid,
        "pmt_pid": pmt_pid,
        "pcr_pid": pcr_pid,
        "frames": frames,
        "pat_version": snap["pat"]["version"] if snap["pat"] else None,
        "pmt_version": pmt.get("version"),
        "ts_packets_by_pid": snap["pid_packets"],
    }


def packetize(ts_bytes, start_seq, ssrc, muxrate_bps):
    """Split TS into RTP packets. Returns packets with identity metadata.

    Mirrors the live sender except that the trailing partial chunk is sent as
    a final short RTP packet instead of being dropped: losing the tail would
    truncate the last picture, and a recording must carry the whole clip.
    """
    total = len(ts_bytes) // 188
    usable_bytes = total * rtp.TS_PACKET_SIZE
    trailing = len(ts_bytes) - usable_bytes
    chunks = [ts_bytes[i:i + rtp.RTP_MTU]
              for i in range(0, usable_bytes, rtp.RTP_MTU)]
    if trailing:
        chunks.append(ts_bytes[usable_bytes:])
    packets = []
    bytes_per_second = muxrate_bps / 8.0
    for i, payload in enumerate(chunks):
        packets.append({
            "index": i,
            "payload": payload,
            "seq16": (start_seq + i) & 0xFFFF,
            "ext_seq": start_seq + i,
            "rtp_ts": int(i * rtp.RTP_MTU / bytes_per_second
                          * rtp.TS_CLOCK_HZ),
            "offset_us": int(i * rtp.RTP_MTU / bytes_per_second * 1e6),
            "ts_pids": [_pid_at(ts_bytes, i * 7 + k)
                        for k in range(len(payload) // 188)],
        })
    return packets, trailing


def _pid_at(ts_bytes, ts_index):
    pkt = ts_bytes[ts_index * 188:(ts_index + 1) * 188]
    return ((pkt[1] & 0x1F) << 8) | pkt[2]


def attach_frames(packets, content_index):
    """Map each RTP packet to the video frame whose PES starts inside it."""
    frame_starts = []
    for frame in content_index["frames"]:
        frame_starts.append((frame["ts_packet_index"] // 7, frame["index"]))
    frame_starts.sort()
    out = []
    cursor = 0
    current = None
    for i, packet in enumerate(packets):
        while cursor < len(frame_starts) and frame_starts[cursor][0] <= i:
            current = frame_starts[cursor][1]
            cursor += 1
        out.append(current)
    for packet, frame in zip(packets, out):
        packet["frame"] = frame
    return packets


def apply_faults(packets, plan, reorder_timeout):
    """Build the wire emission list from a declared fault plan.

    plan entries:
      {"action":"drop","at_ms":2000,"count":30,"reason":"..."}
      {"action":"duplicate","at_ms":3000,"count":8,"delay_ms":90}
      {"action":"reorder","at_ms":4000,"count":48,"hold_count":12,
       "delay_ms":150}
    """
    _validate_plan(plan, packets, reorder_timeout)
    emissions = []
    dropped = set()
    dup_copies = []
    delayed = set()
    events = []
    packets_per_ms = len(packets) / (
        max(p["offset_us"] for p in packets) / 1000.0) if packets else 0

    for fault in plan:
        start = int(fault["at_ms"] * packets_per_ms)
        start = max(0, min(start, len(packets) - 1))
        action = fault["action"]
        if action == "drop":
            indexes = list(range(start, min(start + fault["count"], len(packets))))
            dropped.update(indexes)
            events.append({
                "action": "drop",
                "reason": fault.get("reason", "declared-drop"),
                "at_ms": round(packets[indexes[0]]["offset_us"] / 1000, 3),
                "packet_index": indexes[0],
                "packet_index_end": indexes[-1],
                "seq16": packets[indexes[0]]["seq16"],
                "seq16_end": packets[indexes[-1]]["seq16"],
                "ext_seq": packets[indexes[0]]["ext_seq"],
                "count": len(indexes),
                "frame": packets[indexes[0]]["frame"],
                "frame_end": packets[indexes[-1]]["frame"],
            })
        elif action == "duplicate":
            indexes = list(range(start, min(start + fault["count"], len(packets))))
            delay_us = int(fault.get("delay_ms", 90) * 1000)
            for idx in indexes:
                dup_copies.append((packets[idx]["offset_us"] + delay_us, idx,
                                   fault.get("reason", "declared-duplicate")))
            events.append({
                "action": "duplicate",
                "reason": fault.get("reason", "declared-duplicate"),
                "at_ms": round(packets[indexes[0]]["offset_us"] / 1000, 3),
                "packet_index": indexes[0],
                "packet_index_end": indexes[-1],
                "seq16": packets[indexes[0]]["seq16"],
                "seq16_end": packets[indexes[-1]]["seq16"],
                "ext_seq": packets[indexes[0]]["ext_seq"],
                "count": len(indexes),
                "delay_ms": fault.get("delay_ms", 90),
                "frame": packets[indexes[0]]["frame"],
                "frame_end": packets[indexes[-1]]["frame"],
            })
        elif action == "reorder":
            hold = min(fault.get("hold_count", 12), fault["count"])
            delay_us = int(fault.get("delay_ms", 150) * 1000)
            mid = start + (fault["count"] - hold) // 2
            indexes = list(range(mid, min(mid + hold, len(packets))))
            for idx in indexes:
                delayed.add(idx)
            for k, idx in enumerate(indexes):
                packets[idx]["_wire_offset_us"] = (
                    packets[idx]["offset_us"] + delay_us)
            events.append({
                "action": "reorder",
                "reason": fault.get("reason", "declared-reorder"),
                "at_ms": round(packets[indexes[0]]["offset_us"] / 1000, 3),
                "packet_index": indexes[0],
                "packet_index_end": indexes[-1],
                "seq16": packets[indexes[0]]["seq16"],
                "seq16_end": packets[indexes[-1]]["seq16"],
                "ext_seq": packets[indexes[0]]["ext_seq"],
                "count": len(indexes),
                "delay_ms": fault.get("delay_ms", 150),
                "frame": packets[indexes[0]]["frame"],
                "frame_end": packets[indexes[-1]]["frame"],
            })

    records = []
    for packet in packets:
        if packet["index"] in dropped:
            continue
        offset = packet.get("_wire_offset_us", packet["offset_us"])
        records.append((offset, packet["index"], packet, "delayed"
                        if packet["index"] in delayed else "sent"))
    for offset_us, idx, reason in dup_copies:
        records.append((offset_us, idx, packets[idx], "duplicate-copy"))
    records.sort(key=lambda r: (r[0], r[1]))
    emissions = [{"offset_us": r[0], "index": r[1], "packet": r[2],
                  "wire_action": r[3]} for r in records]
    for packet in packets:
        packet.pop("_wire_offset_us", None)
    return emissions, events, {
        "dropped_packets": sum(1 for p in packets if p["index"] in dropped),
        "duplicate_copies": len(dup_copies),
        "delayed_packets": len(delayed),
        "wire_packets": len(emissions),
        "original_packets": len(packets),
    }


def _validate_plan(plan, packets, reorder_timeout):
    for fault in plan:
        action = fault.get("action")
        if action not in ("drop", "duplicate", "reorder"):
            raise ValueError(f"unknown fault action {action!r}")
        if not isinstance(fault.get("at_ms"), (int, float)):
            raise ValueError("fault is missing at_ms")
        count = fault.get("count")
        if not isinstance(count, int) or count <= 0:
            raise ValueError("fault count must be a positive integer")
        if action == "reorder":
            delay = fault.get("delay_ms", 150) / 1000.0
            if delay >= reorder_timeout:
                raise ValueError(
                    "reorder delay must stay inside reorder_timeout so the "
                    "receiver can actually recover it")
        if action == "duplicate" and fault.get("delay_ms", 90) >= 1000:
            raise ValueError("duplicate delay is unreasonable")


def write_packet_csv(path, packets, emissions, dropped):
    wire_rows = {}
    for order, item in enumerate(emissions):
        wire_rows.setdefault(item["index"], []).append(
            (order, item["offset_us"], item["wire_action"]))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("packet_index,seq16,ext_seq,rtp_ts,frame,ts_pids,"
                 "nominal_ms,wire_order,wire_ms,action,payload_bytes\n")
        for packet in packets:
            rows = wire_rows.get(packet["index"])
            if not rows:
                fh.write(
                    f"{packet['index']},{packet['seq16']},{packet['ext_seq']},"
                    f"{packet['rtp_ts']},{packet['frame']},"
                    f"\"{','.join(map(str, packet['ts_pids']))}\","
                    f"{packet['offset_us'] / 1000:.3f},,,drop,"
                    f"{len(packet['payload'])}\n")
                continue
            for order, offset_us, action in rows:
                fh.write(
                    f"{packet['index']},{packet['seq16']},{packet['ext_seq']},"
                    f"{packet['rtp_ts']},{packet['frame']},"
                    f"\"{','.join(map(str, packet['ts_pids']))}\","
                    f"{packet['offset_us'] / 1000:.3f},{order},"
                    f"{offset_us / 1000:.3f},{action},"
                    f"{len(packet['payload'])}\n")


def manifest_path_for(asset_path):
    return Path(asset_path).with_suffix(".json")


def write_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
