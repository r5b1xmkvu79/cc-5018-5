"""Replay a recorded RTP wire through the live UDP path, then check evidence.

The replay driver emits each recorded datagram at its recorded offset (scaled
by a speed factor). The existing Receiver listens on the same socket as in
live use, so reorder buffer, watchdog, null fill, TS analysis and ffmpeg
decode all run unmodified. Evidence compares what the asset declared with
what the receiver actually observed and decoded.
"""
import collections
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import audio, frames, wire
from ..receiver import Receiver

REPLAY_HOST = "127.0.0.1"


class ReplayDriver:
    """Send a wire file to a UDP port at declared timestamps."""

    def __init__(self, wire_path, host=REPLAY_HOST, port=5004, speed=1.0):
        self.wire_path = Path(wire_path)
        self.dest = (host, port)
        self.speed = max(0.25, min(8.0, float(speed)))
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._stop = threading.Event()
        self._thread = None
        self.reorder_packets = 0      # withhold this many packets, release late
        self.duplicate_packets = 0    # send this many extra copies
        # live injection must not hit the opening packets: that would move the
        # receiver's first-packet base instead of producing reordering.
        self.injection_start_ms = 1000.0
        self._injected = {"reorder": 0, "duplicate": 0}
        self.stats = {"records": 0, "sent": 0, "bytes": 0,
                      "started_at": None, "finished_at": None,
                      "elapsed": 0.0, "speed": self.speed,
                      "injected_reorder": 0, "injected_duplicate": 0,
                      "injection_effect": None}

    def reconfigure(self, reorder_packets=None, duplicate_packets=None):
        if reorder_packets is not None:
            self.reorder_packets = max(0, min(64, int(reorder_packets)))
        if duplicate_packets is not None:
            self.duplicate_packets = max(0, min(64, int(duplicate_packets)))

    def _decide_injection(self, offset_us, index, packet):
        """Live impairment on top of the recorded wire.

        reorder: withhold this datagram and release it 120 ms later, so the
        following packets genuinely overtake it (inside the 250 ms recovery
        window). duplicate: send it once more 80 ms later. Both produce real
        datagrams on the same socket; returns (extra_entries, suppress).
        """
        extra, suppress = [], False
        if offset_us / 1000.0 < self.injection_start_ms:
            return extra, suppress
        if (self.reorder_packets > 0
                and self._injected["reorder"] < self.reorder_packets):
            self._injected["reorder"] += 1
            self.stats["injected_reorder"] = self._injected["reorder"]
            extra.append((offset_us + 120_000, packet))
            suppress = True
            self.stats["injection_effect"] = (
                "reorder: packet withheld 120 ms; later packets overtake it")
        if (self.duplicate_packets > 0
                and self._injected["duplicate"] < self.duplicate_packets):
            self._injected["duplicate"] += 1
            self.stats["injected_duplicate"] = self._injected["duplicate"]
            extra.append((offset_us + 80_000, packet))
            if not suppress:
                self.stats["injection_effect"] = (
                    "duplicate: same packet re-sent 80 ms later")
        return extra, suppress

    def start(self, at=None):
        if at is not None:
            self.stats["started_at"] = at
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        t0 = time.monotonic()
        offset_origin = None
        pending = []                  # (target_seconds, packet) injections
        for offset_us, index, packet in wire.read_wire(self.wire_path):
            if self._stop.is_set():
                break
            if offset_origin is None:
                offset_origin = offset_us
            extra, suppress = self._decide_injection(offset_us, index, packet)
            for extra_offset, extra_packet in extra:
                pending.append((
                    (extra_offset - offset_origin) / 1e6 / self.speed,
                    extra_packet))
            pending.sort(key=lambda item: item[0])
            now = time.monotonic() - t0
            while pending and pending[0][0] <= now:
                self._emit(pending.pop(0)[1])
            target = (offset_us - offset_origin) / 1e6 / self.speed
            delay = target - (time.monotonic() - t0)
            if delay > 0 and self._stop.wait(delay):
                break
            if not suppress:
                self._emit(packet)
            self.stats["records"] = index + 1
        for _, packet in pending:
            if self._stop.is_set():
                break
            self._emit(packet)
        self.stats["elapsed"] = round(time.monotonic() - t0, 3)

    def _emit(self, packet):
        try:
            self._sock.sendto(packet, self.dest)
            self.stats["sent"] += 1
            self.stats["bytes"] += len(packet)
        except OSError:
            pass

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._sock.close()

    def join(self, timeout):
        if self._thread:
            self._thread.join(timeout)

    def running(self):
        return bool(self._thread and self._thread.is_alive())


class ReplaySession:
    """Owns one Receiver + ReplayDriver for a loaded demo asset."""

    def __init__(self, base_dir, manifest, port=5004, speed=1.0,
                 capture_dir=None):
        self.base_dir = Path(base_dir)
        self.manifest = manifest
        self.port = port
        self.speed = speed
        self.capture_dir = capture_dir
        asset_path = self.base_dir / "demos" / "assets" / manifest["wire"]["file"]
        self.wire_path = asset_path
        self.receiver = Receiver({"port": port}, capture_dir=capture_dir)
        self.driver = ReplayDriver(asset_path, port=port, speed=speed)
        self.started_at = None
        self.finished_at = None
        # delayed injections can outlive the last wire packet by up to 120ms;
        # waiting past that lets the watchdog close the final gap cleanly.
        self.settle_seconds = 0.6

    def start(self):
        self.receiver.start()
        time.sleep(0.25)  # port bound before first datagram
        self.started_at = time.monotonic()
        self.driver.start(at=self.started_at)

    def stop(self):
        self.driver.stop()
        self.receiver.stop()

    def join(self):
        """Wait for the replay to finish plus settle time for the watchdog.

        Once the last datagram has been delivered the receiver is closed, so
        any browser fMP4 connection ends instead of hanging forever on an
        idle pipe, and the capture file is final before verification.
        """
        if self.driver._thread:
            self.driver._thread.join(
                timeout=self.manifest["wire"]["duration_ms"] / 1000.0
                / self.speed + 15)
        time.sleep(self.settle_seconds)
        self.receiver.stop()
        self.finished_at = time.monotonic()

    def state(self):
        rx = self.receiver.snapshot()
        return {
            "asset": {"id": self.manifest["id"],
                      "label": self.manifest["label"],
                      "program": self.manifest["program"]["label"],
                      "program_number": self.manifest["program"]["number"],
                      "duration_ms": self.manifest["wire"]["duration_ms"]},
            "replay": {
                "running": self.driver.running(),
                "speed": self.speed,
                "sent": self.driver.stats["sent"],
                "records": self.driver.stats["records"],
                "elapsed": self.driver.stats["elapsed"],
                "injected_reorder": self.driver.stats["injected_reorder"],
                "injected_duplicate": self.driver.stats["injected_duplicate"],
                "injection_effect": self.driver.stats["injection_effect"],
                "finished": (self.finished_at is not None
                             and not self.driver.running()),
            },
            "content": {
                "frame_count": self.manifest["content"]["frame_count"],
                "sha256": self.manifest["content"]["sha256"],
                "file": self.manifest["content"]["file"],
            },
            "manifest": {
                "fault_events": self.manifest["fault_events"],
                "expected": self.manifest["expected"],
                "summary": self.manifest["summary"],
                "wire_sha256": self.manifest["wire"]["sha256"],
            },
            "receiver": rx,
        }


def _run_ffmpeg(args, timeout=60):
    return subprocess.run(args, capture_output=True, timeout=timeout)


def verify_capture(capture_path, content_manifest, ffmpeg, ffprobe):
    """Independent checks over what the receiver actually wrote to disk."""
    capture_path = Path(capture_path)
    result = {"capture": capture_path.name,
              "bytes": capture_path.stat().st_size}
    result["sha256"] = wire.sha256_file(capture_path)

    probe = _run_ffmpeg([
        ffprobe, "-v", "error", "-count_frames",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,nb_read_frames,r_frame_rate",
        "-show_entries", "format=duration,size",
        "-of", "json", str(capture_path)])
    result["ffprobe_rc"] = probe.returncode
    try:
        import json
        result["probe"] = json.loads(probe.stdout or b"{}")
    except ValueError:
        result["probe"] = {}

    decode = _run_ffmpeg([
        ffmpeg, "-v", "error", "-i", str(capture_path),
        "-map", "0", "-c", "copy", "-f", "null", "-"])
    result["decode_rc"] = decode.returncode
    result["decode_stderr_lines"] = [
        line for line in decode.stderr.decode(errors="replace").splitlines()
        if line.strip()][:20]

    raw = _run_ffmpeg([
        ffmpeg, "-v", "error", "-i", str(capture_path), "-map", "0:v",
        "-fps_mode", "passthrough", "-pix_fmt", "gray",
        "-f", "rawvideo", "pipe:1"], timeout=120)
    result["rawvideo_rc"] = raw.returncode
    data = raw.stdout
    size = frames.WIDTH * frames.HEIGHT
    count = len(data) // size
    result["decoded_frames"] = count
    barcodes = []
    for i in range(count):
        decoded = frames.read_barcode(data[i * size:(i + 1) * size])
        barcodes.append(decoded)
    result["barcodes_read"] = sum(1 for b in barcodes if b is not None)
    result["first_barcodes"] = [
        {"decode_index": i, "program": b[0], "frame": b[1]}
        for i, b in enumerate(barcodes[:5]) if b]
    result["last_barcodes"] = [
        {"decode_index": i, "program": b[0], "frame": b[1]}
        for i, b in enumerate(barcodes[-5:]) if b]
    expected_program = content_manifest["program"]["number"]
    program_ok = all(b[0] == expected_program for b in barcodes if b)
    monotone = all(
        barcodes[i][1] < barcodes[i + 1][1]
        for i in range(len(barcodes) - 1)
        if barcodes[i] and barcodes[i + 1])
    result["program_identity_ok"] = program_ok
    result["frame_numbers_monotonic"] = monotone
    result["frame_numbers"] = [b[1] for b in barcodes if b][:400]

    wav = capture_path.with_suffix(".audio.wav")
    extract = _run_ffmpeg([
        ffmpeg, "-y", "-v", "error", "-i", str(capture_path),
        "-map", "0:a:0", "-ac", "1", "-ar", str(audio.SAMPLE_RATE),
        "-f", "wav", str(wav)])
    result["audio_extract_rc"] = extract.returncode
    if extract.returncode == 0 and wav.exists():
        rate, pcm = audio.read_pcm16(wav)
        result["audio_markers"] = audio.detect_markers(pcm, rate)
        wav.unlink(missing_ok=True)
    else:
        result["audio_markers"] = []
    return result


def compare_declared(manifest, receiver_snapshot, verification,
                     ffmpeg=None, content_manifest=None, injection=None):
    """Turn declared intent + observed receiver state into checks.

    `injection` carries live extra datagrams injected on top of the recorded
    wire, so the expected counts are raised by exactly the number of
    additional datagrams rather than loosening the check.
    """
    injection = injection or {}
    extra_reorder = int(injection.get("reorder_packets") or 0)
    extra_dup = int(injection.get("duplicate_packets") or 0)
    stats = receiver_snapshot["stats"]
    ts = receiver_snapshot["ts"]
    expected = manifest["expected"]
    summary = manifest["summary"]

    declared_gap_packets = summary["dropped_packets"]
    # reorder delay is inside the receiver timeout, so reordered packets are
    # recovered rather than counted missing; duplicates must be counted.
    checks = []

    def add(key, actual, want, passed, note=""):
        checks.append({"key": key, "actual": actual, "expected": want,
                       "passed": bool(passed), "note": note})

    add("wire_sent", stats["datagrams"],
        summary["wire_packets"] + extra_dup,
        stats["datagrams"] == summary["wire_packets"] + extra_dup,
        "receiver datagram count equals wire packet records plus any extra "
        "duplicate copies; withheld packets are re-sent, not added")
    add("duplicates", stats["duplicates"],
        summary["duplicate_copies"] + extra_dup,
        stats["duplicates"] == summary["duplicate_copies"] + extra_dup)
    add("out_of_order", stats["out_of_order"],
        summary["delayed_packets"] + extra_reorder,
        stats["out_of_order"] >= summary["delayed_packets"] + extra_reorder,
        "at least the declared reordered packets must be seen as reorder")
    add("missing_packets", stats["missing_packets"], declared_gap_packets,
        stats["missing_packets"] == declared_gap_packets,
        "dropped RTP packets must be marked exactly once")
    add("gap_intervals", stats["gap_intervals"], expected["loss_runs"],
        stats["gap_intervals"] == expected["loss_runs"],
        "each burst should collapse into one recoverable interval")
    add("null_ts_inserted", stats["null_ts_inserted"],
        declared_gap_packets * 7,
        stats["null_ts_inserted"] == declared_gap_packets * 7,
        "one null TS packet per lost TS packet keeps the byte clock")
    add("cc_errors", ts["cc_errors"], manifest["expected"]["cc_errors"],
        ts["cc_errors"] == manifest["expected"]["cc_errors"],
        "continuity errors are declared by the builder, recomputed from the "
        "same packet sequence with the same analyzer")
    add("decode_ok", verification.get("decode_rc"), 0,
        verification.get("decode_rc") == 0,
        "ffmpeg must decode the whole captured TS")

    if content_manifest:
        add("decoded_frames_ge_half",
            verification.get("decoded_frames"), content_manifest["frame_count"],
            (verification.get("decoded_frames") or 0)
            >= content_manifest["frame_count"] // 2,
            "some frames may be lost inside a declared outage")
        # frames whose PES is cut by a declared drop can still be emitted by
        # the decoder but carry no intact picture; require every frame of a
        # clean wire to be readable, and most frames of a lossy wire.
        clean = summary["dropped_packets"] == 0
        floor = (verification.get("decoded_frames") or 0) if clean else \
            int((verification.get("decoded_frames") or 0) * 0.9)
        add("barcode_readable", verification.get("barcodes_read"),
            floor, (verification.get("barcodes_read") or 0) >= floor,
            "every decoded picture must carry a valid frame barcode"
            if clean else "damaged frames may lose their barcode; >=90% must "
                          "still identify picture and program")
        add("program_number", content_manifest["program"]["number"],
            content_manifest["program"]["number"],
            verification.get("program_identity_ok") is True,
            "all readable barcodes carry the declared program number")
        add("frame_order", "monotonic",
            "monotonic", verification.get("frame_numbers_monotonic") is True,
            "picture numbers must advance in decode order")
        add("audio_markers", len(verification.get("audio_markers") or []),
            ">0", bool(verification.get("audio_markers")),
            "audio marker pulses must survive the transport")

    return checks
