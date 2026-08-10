"""Generate the checked-in, non-sensitive food-Vision v1 PNG fixtures.

This file is provenance for the committed raster assets; normal harness runs
only verify their manifest hashes and never regenerate or download fixtures.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path


WIDTH = 480
HEIGHT = 320


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _write_png(path: Path, pixels: bytearray) -> None:
    rows = b"".join(b"\x00" + bytes(pixels[row * WIDTH * 3:(row + 1) * WIDTH * 3]) for row in range(HEIGHT))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(rows, level=9))
        + _chunk(b"IEND", b"")
    )


def _canvas() -> bytearray:
    return bytearray([248, 245, 237] * WIDTH * HEIGHT)


def _rectangle(pixels: bytearray, left: int, top: int, right: int, bottom: int, color: tuple[int, int, int]) -> None:
    for y in range(max(0, top), min(HEIGHT, bottom)):
        for x in range(max(0, left), min(WIDTH, right)):
            offset = (y * WIDTH + x) * 3
            pixels[offset:offset + 3] = bytes(color)


def _ellipse(pixels: bytearray, center_x: int, center_y: int, radius_x: int, radius_y: int, color: tuple[int, int, int]) -> None:
    for y in range(max(0, center_y - radius_y), min(HEIGHT, center_y + radius_y)):
        for x in range(max(0, center_x - radius_x), min(WIDTH, center_x + radius_x)):
            if ((x - center_x) / radius_x) ** 2 + ((y - center_y) / radius_y) ** 2 <= 1:
                offset = (y * WIDTH + x) * 3
                pixels[offset:offset + 3] = bytes(color)


def _plate(pixels: bytearray) -> None:
    _ellipse(pixels, 240, 165, 185, 110, (224, 231, 235))
    _ellipse(pixels, 240, 165, 160, 92, (255, 255, 255))


def _fixture_a() -> bytearray:
    pixels = _canvas()
    _plate(pixels)
    _ellipse(pixels, 165, 155, 38, 38, (211, 51, 47))  # apple
    _rectangle(pixels, 199, 113, 208, 140, (79, 123, 47))
    _ellipse(pixels, 267, 160, 30, 60, (244, 206, 53))  # banana
    _ellipse(pixels, 335, 165, 35, 55, (221, 160, 78))  # bread
    return pixels


def _fixture_b() -> bytearray:
    pixels = _canvas()
    _plate(pixels)
    _ellipse(pixels, 165, 155, 35, 55, (230, 89, 58))  # carrot
    _ellipse(pixels, 250, 150, 42, 32, (49, 142, 74))  # cucumber
    _ellipse(pixels, 330, 165, 35, 42, (236, 214, 94))  # cheese
    _rectangle(pixels, 365, 90, 425, 120, (88, 131, 211))  # distractor cup
    return pixels


def _fixture_c() -> bytearray:
    pixels = _canvas()
    _plate(pixels)
    _ellipse(pixels, 155, 170, 36, 50, (220, 48, 43))  # ketchup
    _ellipse(pixels, 245, 170, 36, 50, (239, 194, 40))  # mustard
    _ellipse(pixels, 335, 170, 36, 50, (244, 240, 224))  # sour cream
    return pixels


def main() -> None:
    root = Path(__file__).parent / "images"
    root.mkdir(exist_ok=True)
    for name, painter in (("fixture_a", _fixture_a), ("fixture_b", _fixture_b), ("fixture_c", _fixture_c)):
        _write_png(root / f"{name}.png", painter())


if __name__ == "__main__":
    main()
