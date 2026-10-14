#!/usr/bin/env python3
"""Regenerate the Stream Deck plugin icons (stdlib only; output is byte-deterministic)."""
import os
import struct
import sys
import zlib

BG = (0x21, 0x3A, 0x4E)
FG = (255, 255, 255)
GLYPHS = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "src", "quoin", "streamdeck",
                   "payload", "plugin")


def png(width, height, rows):
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def render(size):
    pixels = [[BG] * size for _ in range(size)]
    lines = ["AGENT", "DESK"] if size >= 36 else ["AD"]
    scale = max(1, size // 36)
    gap = scale
    line_h = 7 * scale
    total_h = len(lines) * line_h + (len(lines) - 1) * 2 * scale
    top = (size - total_h) // 2
    for li, text in enumerate(lines):
        width = len(text) * 5 * scale + (len(text) - 1) * gap
        left = (size - width) // 2
        y0 = top + li * (line_h + 2 * scale)
        for ci, ch in enumerate(text):
            x0 = left + ci * (5 * scale + gap)
            for ry, row in enumerate(GLYPHS[ch]):
                for rx, bit in enumerate(row):
                    if bit == "1":
                        for dy in range(scale):
                            for dx in range(scale):
                                pixels[y0 + ry * scale + dy][x0 + rx * scale + dx] = FG
    return png(size, size, [[c for px in row for c in px] for row in pixels])


def main(out=OUT):
    out = os.path.abspath(out)
    for name, size in (("icon.png", 144), ("icon@2x.png", 288), ("category.png", 28)):
        with open(os.path.join(out, name), "wb") as handle:
            handle.write(render(size))


if __name__ == "__main__":
    main(*sys.argv[1:2])
