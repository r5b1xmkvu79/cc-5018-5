"""Generate demo broadcast programs: unique frame numbers + audio markers.

A program is a real MPEG-TS clip made from generated grayscale pictures
(each carrying a machine-readable frame barcode and human-readable number)
and a generated marker tone. Content manifests record the exact ffmpeg
command, every source parameter, hashes and the frame/audio marker maps, so
the clip can be rebuilt and audited instead of trusted as a screenshot.
"""
import subprocess
import sys
import threading
from pathlib import Path

from . import audio, frames
from .wire import (index_content, read_json, sha256_file, write_json)

DEMO_ROOT = Path(__file__).resolve().parent.parent.parent / "demos"
PROGRAMS_DIR = DEMO_ROOT / "programs"

# Three visually distinct programs; the barcode program number is the
# authoritative identity, the picture style only helps a human tell them apart.
PROGRAMS = [
    {
        "id": "campus-news", "number": 1, "label": "园区新闻 (Campus News)",
        "style": 0, "fps": 25, "duration_s": 10.0,
        "video_bps": 700_000, "audio_bps": 64_000, "muxrate": 900_000,
        "service_id": 1001, "transport_stream_id": 101,
    },
    {
        "id": "campus-life", "number": 2, "label": "园区生活 (Campus Life)",
        "style": 1, "fps": 25, "duration_s": 10.0,
        "video_bps": 700_000, "audio_bps": 64_000, "muxrate": 900_000,
        "service_id": 1002, "transport_stream_id": 102,
    },
    {
        "id": "campus-tts", "number": 3, "label": "图文频道 (Info Ticker)",
        "style": 2, "fps": 25, "duration_s": 10.0,
        "video_bps": 600_000, "audio_bps": 48_000, "muxrate": 800_000,
        "service_id": 1003, "transport_stream_id": 103,
    },
]


def find_ffmpeg():
    here = Path(__file__).resolve().parent.parent.parent
    candidate = here / "tools" / "ffmpeg-static" / "ffmpeg"
    return str(candidate) if candidate.exists() else "ffmpeg"


def ffmpeg_version(ffmpeg):
    out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True)
    return out.stdout.splitlines()[0].strip() if out.stdout else "unknown"


def _style_shift(style, y, x, index):
    if style == 0:
        return (y * 2 + index * 5 + x // 6) & 0xFF
    if style == 1:
        return (x // 2 + index * 7 + y // 4) & 0xFF
    return (((x // 32) + (y // 32) + index // 5) & 1) * 90 + (index * 3) % 60


def render_program_frames(program, output):
    """Write every frame to a sink callable; returns per-frame metadata."""
    total = int(round(program["duration_s"] * program["fps"]))
    meta = []
    for index in range(total):
        frame = frames.render_frame(program["number"], index)
        # style pass: replace the plain ramp with the program's pattern
        # while keeping the barcode/text area untouched
        view = bytearray(frame)
        for y in range(0, 264):
            row = y * frames.WIDTH
            for x in range(0, frames.WIDTH):
                view[row + x] = _style_shift(program["style"], y, x, index)
        output(bytes(view))
        meta.append({"index": index, "program": program["number"],
                     "fps": program["fps"]})
    return meta


def build_program(program, force=False):
    out_dir = PROGRAMS_DIR / program["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    ts_path = out_dir / "content.ts"
    wav_path = out_dir / "marker.wav"
    manifest_path = out_dir / "content.json"
    if ts_path.exists() and manifest_path.exists() and not force:
        return read_json(manifest_path), False

    ffmpeg = find_ffmpeg()
    version = ffmpeg_version(ffmpeg)
    audio_info = audio.write_wav(wav_path, program["duration_s"])
    total_frames = int(round(program["duration_s"] * program["fps"]))

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-threads", "1", "-fflags", "+bitexact", "-flags", "+bitexact",
        "-f", "rawvideo", "-framerate", str(program["fps"]),
        "-pix_fmt", "gray", "-s", f"{frames.WIDTH}x{frames.HEIGHT}",
        "-i", "pipe:0",
        "-i", str(wav_path),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-g", str(program["fps"]), "-b:v", str(program["video_bps"]),
        "-x264-params", "threads=1:sliced-threads=0",
        "-c:a", "aac", "-b:a", str(program["audio_bps"]),
        "-muxrate", str(program["muxrate"]),
        "-mpegts_flags", "+pat_pmt_at_frames", "-pcr_period", "40",
        "-mpegts_service_id", str(program["service_id"]),
        "-mpegts_transport_stream_id", str(program["transport_stream_id"]),
        "-tables_version", "0", "-map_metadata", "-1",
        "-f", "mpegts", str(ts_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    err = []

    def feed():
        try:
            render_program_frames(
                program, lambda frame: proc.stdin.write(frame))
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    feeder = threading.Thread(target=feed)
    feeder.start()
    stderr = proc.stderr.read().decode(errors="replace")
    rc = proc.wait()
    feeder.join()
    if rc != 0:
        raise RuntimeError(f"ffmpeg failed for {program['id']}: {stderr}")

    data = ts_path.read_bytes()
    index = index_content(data)
    if len(index["frames"]) != total_frames:
        raise RuntimeError(
            f"{program['id']}: expected {total_frames} frames, "
            f"indexed {len(index['frames'])}")
    if index["frames"][0]["index"] != 0:
        raise RuntimeError("frame numbering does not start at zero")

    markers = [{"second": s,
                "kind": "mark" if s % 5 == 0 else "tick",
                "at_ms": s * 1000,
                "tone_hz": audio.MARK_HZ if s % 5 == 0 else audio.TONE_HZ}
               for s in range(int(program["duration_s"]))]

    manifest = {
        "schema": "tvcast.demo.content/1",
        "program": {k: program[k] for k in
                    ("id", "number", "label", "style", "fps",
                     "duration_s", "video_bps", "audio_bps", "muxrate",
                     "service_id", "transport_stream_id")},
        "source": {
            "generator": "tvcast.demo.frames.render_frame",
            "frame_width": frames.WIDTH,
            "frame_height": frames.HEIGHT,
            "frame_pix_fmt": "gray",
            "barcode": {"bits": 28, "crc": "crc8/0x07",
                        "layout": "sync4+program4+index12+crc8",
                        "position": {"x": frames.BAR_X, "y": frames.BAR_Y,
                                     "bar_w": frames.BAR_W,
                                     "gap": frames.BAR_GAP,
                                     "height": frames.BAR_H}},
            "audio": audio_info,
            "ffmpeg": {"version": version, "command": cmd},
        },
        "file": "content.ts",
        "sha256": sha256_file(ts_path),
        "bytes": len(data),
        "duration_ms": int(round(program["duration_s"] * 1000)),
        "frame_count": total_frames,
        "index": index,
        "audio_markers": markers,
        "marker_wav": {"file": "marker.wav",
                       "sha256": sha256_file(wav_path)},
    }
    write_json(manifest_path, manifest)
    return manifest, True


def build_all(force=False, only=None):
    results = []
    for program in PROGRAMS:
        if only and program["id"] not in only:
            continue
        manifest, written = build_program(program, force=force)
        results.append((program["id"], written, manifest))
        print(f"content {'built' if written else 'present'}: "
              f"{program['id']} "
              f"({manifest['frame_count']} frames, "
              f"{manifest['bytes']} bytes)")
    return results


if __name__ == "__main__":
    build_all(force="--force" in sys.argv)
