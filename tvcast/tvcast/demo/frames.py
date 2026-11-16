"""Deterministic test-picture frames: unique frame number + program id.

Each frame is a raw 8-bit grayscale image. A 28-bit barcode at the bottom
encodes the program number and the frame index with an 8-bit CRC, so the
frame number can be read back from pixels by both the verifier and a human.
The frame number also appears as human-readable digits drawn with a small
built-in 5x7 font (no system font or drawtext filter is required).
"""
import struct
import zlib

WIDTH = 640
HEIGHT = 360
FPS = 25

FONT = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11111", "00010", "00100", "00010", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    ":": ["00000", "00100", "00100", "00000", "00100", "00100", "00000"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    " ": ["00000"] * 7,
}

BAR_X = 20
BAR_Y = 318
BAR_W = 8
BAR_GAP = 2
BAR_H = 28
BAR_COUNT = 56           # 28 bits, each bit two bars wide
CRC8_POLY = 0x07


def crc8(bits):
    v = 0
    for b in bits:
        v ^= b << 7
        for _ in range(8):
            v = ((v << 1) ^ CRC8_POLY) & 0xFF if v & 0x80 else (v << 1) & 0xFF
    return v


def frame_bits(program, index):
    """28 bits: sync(4) + program(4) + index(12) + crc8."""
    payload = [(program >> i) & 1 for i in (3, 2, 1, 0)]
    payload += [(index >> i) & 1 for i in range(11, -1, -1)]
    crc = crc8(payload)
    return [1, 0, 1, 0] + payload + [(crc >> i) & 1 for i in range(7, -1, -1)]


def _draw_rect(buf, x0, y0, w, h, value):
    row = bytes([value]) * w
    for y in range(y0, min(y0 + h, HEIGHT)):
        buf[y * WIDTH + x0:y * WIDTH + x0 + w] = row


def render_frame(program, index):
    """Return one raw grayscale frame (WIDTH*HEIGHT bytes)."""
    buf = bytearray(WIDTH * HEIGHT)
    # Luma ramp with a moving diagonal stripe: not a static screenshot,
    # and the motion lets a human confirm playback is live.
    for y in range(HEIGHT):
        shift = (y * 2 + index * 5) % 256
        buf[y * WIDTH:(y + 1) * WIDTH] = bytes(
            (shift + (x // 6)) & 0xFF for x in range(WIDTH))
    _draw_rect(buf, 10, 266, WIDTH - 20, 84, 10)
    _draw_rect(buf, 10, 266, WIDTH - 20, 2, 220)
    text = f"{program:02d}-{index:06d}"
    x, y = 22, 278
    scale = 4
    for ch in text:
        glyph = FONT.get(ch, FONT[" "])
        for ry, row in enumerate(glyph):
            for rx, bit in enumerate(row):
                if bit == "1":
                    _draw_rect(buf, x + rx * scale, y + ry * scale,
                               scale, scale, 235)
        x += 6 * scale
    bx = BAR_X
    for bit in frame_bits(program, index):
        for _ in range(2):
            _draw_rect(buf, bx, BAR_Y, BAR_W, BAR_H, 235 if bit else 18)
            bx += BAR_W + BAR_GAP
    return bytes(buf)


def read_barcode(frame):
    """Decode the barcode from a raw frame. Returns (program, index) or None."""
    if len(frame) < WIDTH * HEIGHT:
        return None
    raw = []
    for i in range(BAR_COUNT):
        x0 = BAR_X + i * (BAR_W + BAR_GAP)
        samples = [frame[(BAR_Y + dy) * WIDTH + x0 + dx]
                   for dx in (2, 4, 6) for dy in (6, 13, 20)]
        raw.append(1 if sum(samples) / len(samples) > 120 else 0)
    bits = [1 if raw[2 * i] + raw[2 * i + 1] >= 1 else 0
            for i in range(BAR_COUNT // 2)]
    if bits[:4] != [1, 0, 1, 0]:
        return None
    payload, crc_bits = bits[4:20], bits[20:28]
    if crc8(payload) != int("".join(map(str, crc_bits)), 2):
        return None
    program = int("".join(map(str, payload[:4])), 2)
    index = int("".join(map(str, payload[4:])), 2)
    return program, index


def write_pgm(path, frame):
    """Debug helper: dump a frame as binary PGM (no external library)."""
    with open(path, "wb") as fh:
        fh.write(b"P5\n%d %d\n255\n" % (WIDTH, HEIGHT))
        fh.write(frame)


def png_bytes(frame):
    """Encode one grayscale frame as PNG using only the standard library."""
    raw = b"".join(b"\x00" + frame[y * WIDTH:(y + 1) * WIDTH]
                   for y in range(HEIGHT))
    def chunk(tag, data):
        return (struct.pack("!I", len(data)) + tag + data
                + struct.pack("!I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack("!IIBBBBB", WIDTH, HEIGHT,
                                         8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))
