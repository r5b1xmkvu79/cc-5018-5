"""Audio marker: a deterministic tone pattern tied to the frame timeline.

The marker is a 1 kHz pulse (60 ms) at the start of every second, doubled
at every 5th second (1.5 kHz). A decoder reports which marker slots were
heard, so audio can be matched to the wall-clock seconds of the picture
without listening by ear. All generation/analysis is stdlib only.
"""
import math
import struct
import wave

SAMPLE_RATE = 48000
TONE_HZ = 1000
MARK_HZ = 1500
PULSE_SECONDS = 0.06


def pulse_train(duration, start_offset=0.0):
    """Mono float samples in [-1, 1] with the marker pattern for `duration`."""
    n = int(round(duration * SAMPLE_RATE))
    out = []
    for i in range(n):
        t = start_offset + i / SAMPLE_RATE
        phase = t % 1.0
        v = 0.0
        if phase < PULSE_SECONDS:
            v += 0.30 * math.sin(2 * math.pi * TONE_HZ * t)
        if abs(t % 5.0) < PULSE_SECONDS or abs(t % 5.0 - 0.18) < PULSE_SECONDS:
            v += 0.18 * math.sin(2 * math.pi * MARK_HZ * t)
        out.append(max(-1.0, min(1.0, v)))
    return out


def write_wav(path, duration, start_offset=0.0):
    samples = pulse_train(duration, start_offset)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(
            struct.pack("<h", int(s * 32767)) for s in samples))
    return {"duration": duration, "samples": len(samples),
            "sample_rate": SAMPLE_RATE, "tone_hz": TONE_HZ,
            "mark_hz": MARK_HZ, "pulse_seconds": PULSE_SECONDS}


def read_pcm16(path):
    with wave.open(str(path), "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise ValueError("expected mono 16-bit wav")
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    return rate, struct.unpack("<%dh" % (len(raw) // 2), raw)


def detect_markers(pcm, rate):
    """Locate marker pulses by envelope energy. Returns slot list in seconds.

    Returns [{'t': seconds, 'kind': 'tick'|'mark'}] where 'mark' marks the
    double-pulse (5 s) positions and 'tick' the single-pulse ones. Slots
    are matched to integer seconds so a recording that starts mid-stream
    still yields the offsets present in the stream.
    """
    win = int(rate * 0.01)
    energy = []
    for i in range(0, len(pcm) - win, win):
        block = pcm[i:i + win]
        energy.append(sum(abs(v) for v in block) / win)
    if not energy:
        return []
    peak = max(energy)
    threshold = max(peak * 0.25, 150.0)
    active = [e >= threshold for e in energy]
    slots = []
    i = 0
    while i < len(active):
        if not active[i]:
            i += 1
            continue
        start = i
        while i < len(active) and active[i]:
            i += 1
        start_t = start * win / rate
        # group pulses within a second into one slot; second pulse within
        # the same slot turns it into a 'mark'
        slots.append(start_t)
    merged = []
    for t in slots:
        if merged and t - merged[-1]["t"] < 0.30:
            merged[-1]["kind"] = "mark"
            continue
        merged.append({"t": round(t, 3), "kind": "tick", "second": round(t)})
    return merged
