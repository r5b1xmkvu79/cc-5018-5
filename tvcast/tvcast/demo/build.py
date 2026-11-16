"""Build transport demo assets: content -> RTP wire -> fault plan -> manifest.

Each asset is a *real* recorded RTP wire. The manifest stores the event
timeline (in microseconds) and packet identity (program, frame number,
original index, seq16, ext_seq, RTP timestamp), so the receiver can replay
the wire and the evidence can be mapped back to both time and picture.
"""
from pathlib import Path

from . import wire
from .content import PROGRAMS, build_all, build_program

ASSETS_DIR = Path(__file__).resolve().parent.parent.parent / "demos" / "assets"

# Fault plans are declared up front. Bursts include a multi-second loss
# (larger than the reorder timeout, so the receiver must give up and mark it)
# and reordering that stays inside the timeout so recovery is observable.
ASSETS = [
    {
        "id": "clean",
        "program": "campus-news",
        "label": "清洁基线 · 无损伤",
        "description": "完整 10 秒节目，作为连续性/时钟恢复的对照基线。",
        "start_seq": 65530,
        "ssrc": 0xCAFE0001,
        "faults": [],
        "expected": {"loss_runs": 0},
    },
    {
        "id": "loss-burst",
        "program": "campus-news",
        "label": "连丢包突发 · 恢复",
        "description": "2.0s 起连续丢 35 个 RTP 包（约 23 个视频帧的切片），"
                       "覆盖重排超时后接收端判缺口、后续包继续解码。",
        "start_seq": 65420,
        "ssrc": 0xCAFE0001,
        "faults": [
            {"action": "drop", "at_ms": 2000, "count": 35,
             "reason": "burst-loss-35"},
        ],
        "expected": {"loss_runs": 1},
    },
    {
        "id": "dup-reorder",
        "program": "campus-life",
        "label": "重复 + 乱序突发 · 重排恢复",
        "description": "3.0s 起 10 个包各晚到 90ms 造成重复；4.2s 起 12 个包"
                       "被扣留 150ms 越过后继包，在重排超时内补齐。",
        "start_seq": 65020,
        "ssrc": 0xCAFE0002,
        "faults": [
            {"action": "duplicate", "at_ms": 3000, "count": 10,
             "delay_ms": 90, "reason": "dup-window-10"},
            {"action": "reorder", "at_ms": 4200, "count": 12,
             "hold_count": 12, "delay_ms": 150,
             "reason": "reorder-window-12"},
        ],
        "expected": {"loss_runs": 0},
    },
    {
        "id": "outage-long",
        "program": "campus-tts",
        "label": "超时断流 · 不可恢复区间",
        "description": "2.5s 起连续丢 120 个包（约 1.3s 断流，远超 250ms "
                       "重排超时），接收端必须判出不可恢复区间而非无限等待；"
                       "之后流继续并恢复解码。",
        "start_seq": 65200,
        "ssrc": 0xCAFE0003,
        "faults": [
            {"action": "drop", "at_ms": 2500, "count": 120,
             "reason": "outage-120"},
        ],
        "expected": {"loss_runs": 1},
    },
]


def _program_def(program_id):
    for program in PROGRAMS:
        if program["id"] == program_id:
            return program
    raise ValueError(f"unknown program {program_id!r}")


def build_asset(asset, content_manifest, force=False):
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    base = ASSETS_DIR / asset["id"]
    wire_path = base.with_suffix(".wire")
    manifest_path = base.with_suffix(".json")
    csv_path = base.with_suffix(".packets.csv")
    if wire_path.exists() and manifest_path.exists() and not force:
        return wire.read_json(manifest_path), False

    program_dir = (Path(__file__).resolve().parent.parent.parent
                   / "demos" / "programs" / content_manifest["program"]["id"])
    ts_bytes = (program_dir / content_manifest["file"]).read_bytes()
    index = content_manifest["index"]

    packets, trailing = wire.packetize(
        ts_bytes, asset["start_seq"], asset["ssrc"],
        content_manifest["program"]["muxrate"])
    wire.attach_frames(packets, index)

    emissions, events, summary = wire.apply_faults(
        packets, asset["faults"], reorder_timeout=0.25)

    entries = [(item["offset_us"], item["index"],
                wire.rtp.build_rtp(
                    item["packet"]["seq16"],
                    item["packet"]["rtp_ts"],
                    asset["ssrc"], item["packet"]["payload"]))
               for item in emissions]
    wire.write_wire(wire_path, entries)
    wire.write_packet_csv(csv_path, packets, emissions,
                          {p["index"] for p in packets} - {
                              i["index"] for i in emissions})

    # sanity: every fault stream had its premises checked before writing
    window_ms = packets[-1]["offset_us"] / 1000.0
    content_dir = Path(__file__).resolve().parent.parent.parent
    manifest = {
        "schema": "tvcast.demo.asset/1",
        "id": asset["id"],
        "label": asset["label"],
        "description": asset["description"],
        "program": content_manifest["program"],
        "content": {
            "file": str((program_dir / content_manifest["file"])
                        .relative_to(content_dir)),
            "sha256": content_manifest["sha256"],
            "frame_count": content_manifest["frame_count"],
            "duration_ms": content_manifest["duration_ms"],
            "content_manifest": str(
                (program_dir / "content.json").relative_to(content_dir)),
        },
        "wire": {
            "file": wire_path.name,
            "sha256": wire.sha256_file(wire_path),
            "bytes": wire_path.stat().st_size,
            "packet_records": len(entries),
            "rtp_payload_bytes": sum(len(i["packet"]["payload"])
                                     for i in emissions),
            "start_seq": asset["start_seq"],
            "end_seq": packets[-1]["seq16"],
            "ssrc": asset["ssrc"],
            "muxrate_bps": content_manifest["program"]["muxrate"],
            "duration_ms": round(window_ms, 3),
            "ts_trailing_bytes": trailing,
        },
        "packets_csv": csv_path.name,
        "packet_identity": {
            "seq16": "16-bit RTP sequence carried on the wire",
            "ext_seq": "64-bit extended sequence (start_seq + index)",
            "rtp_ts": "RTP timestamp in 90 kHz units",
            "packet_index": "original packetization index, stable under faults",
            "frame": "video frame number rendered into the picture",
            "program": "program number encoded in the frame barcode",
        },
        "fault_plan": asset["faults"],
        "fault_events": events,
        "summary": summary,
        "expected": {**asset["expected"],
                     **expected_checks(packets, emissions, index)},
        "receiver_contract": {
            "reorder_timeout_ms": 250,
            "max_reorder_packets": 256,
            "reorder_delay_ms": 150,
            "duplicate_delay_ms": 90,
        },
    }
    wire.write_json(manifest_path, manifest)
    return manifest, True


def expected_checks(packets, emissions, content_index):
    """Baseline the builder can derive without running the receiver.

    The receiver hands the analyzer packets in playout order (recovered
    reorder, duplicates discarded), which equals original packet order with
    dropped packets removed. Feeding that same sequence through the same
    TSAnalyzer yields the cc-error count a correct receiver must reproduce.
    """
    from ..ts import TSAnalyzer
    present = {item["index"] for item in emissions}
    analyzer = TSAnalyzer()
    for packet in packets:
        if packet["index"] not in present:
            continue
        payload = packet["payload"]
        for k in range(len(payload) // 188):
            analyzer.feed(payload[k * 188:(k + 1) * 188], 0.0)
    return {"cc_errors": analyzer.cc_errors}


def build_all_assets(force=False, programs=True, only=None):
    if programs:
        build_all(force=force)
    built = []
    for asset in ASSETS:
        if only and asset["id"] not in only:
            continue
        program = _program_def(asset["program"])
        content_manifest, _ = build_program(program)
        manifest, written = build_asset(asset, content_manifest, force=force)
        built.append((asset["id"], written, manifest))
        print(f"asset {'built' if written else 'present'}: {asset['id']} "
              f"({manifest['wire']['packet_records']} wire packets, "
              f"{manifest['summary']['dropped_packets']} dropped, "
              f"{manifest['summary']['duplicate_copies']} dup copies)")
    return built


if __name__ == "__main__":
    import sys
    build_all_assets(force="--force" in sys.argv)
