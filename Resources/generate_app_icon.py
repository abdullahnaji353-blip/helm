#!/usr/bin/env python3
"""Draw Helm's app icon with the Python standard library (no image dependencies)."""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

SIZE = 1024
Pixel = tuple[int, int, int, int]


class Canvas:
    def __init__(self, size: int = SIZE) -> None:
        self.size = size
        self.pixels = bytearray(size * size * 4)
        self._background()

    def put(self, x: int, y: int, color: Pixel) -> None:
        if not (0 <= x < self.size and 0 <= y < self.size):
            return
        offset = (y * self.size + x) * 4
        alpha = color[3] / 255
        if alpha >= 1:
            self.pixels[offset:offset + 4] = bytes(color)
            return
        inverse = 1 - alpha
        for channel in range(3):
            self.pixels[offset + channel] = round(color[channel] * alpha + self.pixels[offset + channel] * inverse)
        self.pixels[offset + 3] = 255

    def _background(self) -> None:
        for y in range(self.size):
            for x in range(self.size):
                # macOS applies its own app-icon mask; this clean square keeps
                # the edge treatment consistent in Finder, Spotlight and Dock.
                t = (x + self.size - y) / (2 * self.size)
                base = (round(20 + 13 * t), round(35 + 18 * t), round(52 + 20 * t))
                dx, dy = x - 512, y - 512
                glow = max(0.0, 1 - math.sqrt(dx * dx + dy * dy) / 640) * 0.14
                color = (round(base[0] + 25 * glow), round(base[1] + 92 * glow), round(base[2] + 93 * glow), 255)
                self.put(x, y, color)

    def circle(self, cx: float, cy: float, radius: float, color: Pixel, width: float = 0) -> None:
        outer = radius + width / 2
        inner = max(0, radius - width / 2)
        for y in range(max(0, int(cy - outer - 1)), min(self.size, int(cy + outer + 2))):
            for x in range(max(0, int(cx - outer - 1)), min(self.size, int(cx + outer + 2))):
                distance = math.hypot(x + 0.5 - cx, y + 0.5 - cy)
                if (width == 0 and distance <= radius) or (width and inner <= distance <= outer):
                    self.put(x, y, color)

    def line(self, start: tuple[float, float], end: tuple[float, float], width: float, color: Pixel) -> None:
        x1, y1 = start
        x2, y2 = end
        left = max(0, int(min(x1, x2) - width))
        right = min(self.size, int(max(x1, x2) + width + 1))
        top = max(0, int(min(y1, y2) - width))
        bottom = min(self.size, int(max(y1, y2) + width + 1))
        vx, vy = x2 - x1, y2 - y1
        length2 = vx * vx + vy * vy
        for y in range(top, bottom):
            for x in range(left, right):
                t = max(0.0, min(1.0, ((x + 0.5 - x1) * vx + (y + 0.5 - y1) * vy) / length2))
                px, py = x1 + t * vx, y1 + t * vy
                if math.hypot(x + 0.5 - px, y + 0.5 - py) <= width / 2:
                    self.put(x, y, color)
        self.circle(x1, y1, width / 2, color)
        self.circle(x2, y2, width / 2, color)

    def polygon(self, points: list[tuple[float, float]], color: Pixel) -> None:
        low = max(0, int(min(y for _, y in points)))
        high = min(self.size, int(max(y for _, y in points)) + 1)
        for y in range(low, high):
            scan = y + 0.5
            crossings: list[float] = []
            for index, (x1, y1) in enumerate(points):
                x2, y2 = points[(index + 1) % len(points)]
                if (y1 <= scan < y2) or (y2 <= scan < y1):
                    crossings.append(x1 + (scan - y1) * (x2 - x1) / (y2 - y1))
            crossings.sort()
            for index in range(0, len(crossings) - 1, 2):
                for x in range(max(0, int(crossings[index])), min(self.size, int(crossings[index + 1]) + 1)):
                    self.put(x, y, color)

    def gradient_polygon(
        self, points: list[tuple[float, float]], top: Pixel, bottom: Pixel
    ) -> None:
        low = max(0, int(min(y for _, y in points)))
        high = min(self.size, int(max(y for _, y in points)) + 1)
        full_height = max(1, max(y for _, y in points) - min(y for _, y in points))
        for y in range(low, high):
            scan = y + 0.5
            crossings: list[float] = []
            for index, (x1, y1) in enumerate(points):
                x2, y2 = points[(index + 1) % len(points)]
                if (y1 <= scan < y2) or (y2 <= scan < y1):
                    crossings.append(x1 + (scan - y1) * (x2 - x1) / (y2 - y1))
            crossings.sort()
            amount = max(0.0, min(1.0, (y - min(point[1] for point in points)) / full_height))
            color = tuple(round(a + (b - a) * amount) for a, b in zip(top, bottom))
            for index in range(0, len(crossings) - 1, 2):
                for x in range(max(0, int(crossings[index])), min(self.size, int(crossings[index + 1]) + 1)):
                    self.put(x, y, color)  # type: ignore[arg-type]

    def save_png(self, path: Path) -> None:
        raw = bytearray()
        row_bytes = self.size * 4
        for y in range(self.size):
            raw.append(0)
            start = y * row_bytes
            raw.extend(self.pixels[start:start + row_bytes])

        def chunk(kind: bytes, data: bytes) -> bytes:
            body = kind + data
            return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

        png = b"\x89PNG\r\n\x1a\n"
        png += chunk(b"IHDR", struct.pack(">2I5B", self.size, self.size, 8, 6, 0, 0, 0))
        png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        png += chunk(b"IEND", b"")
        path.write_bytes(png)

    def save_icns(self, path: Path) -> None:
        chunk_types = [
            (16, b"icp4"), (32, b"icp5"), (64, b"icp6"),
            (128, b"ic07"), (256, b"ic08"), (512, b"ic09"), (1024, b"ic10"),
        ]
        with tempfile.TemporaryDirectory(prefix="helm-icon-") as folder:
            temporary = Path(folder)
            full_size = temporary / "icon_1024.png"
            self.save_png(full_size)
            payload = bytearray()
            for size, chunk_type in chunk_types:
                image = temporary / f"icon_{size}.png"
                if size == SIZE:
                    image = full_size
                else:
                    subprocess.run(
                        ["sips", "-z", str(size), str(size), str(full_size), "--out", str(image)],
                        check=True,
                        stdout=subprocess.DEVNULL,
                    )
                data = image.read_bytes()
                payload.extend(chunk_type + struct.pack(">I", len(data) + 8) + data)
            path.write_bytes(b"icns" + struct.pack(">I", len(payload) + 8) + payload)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: generate_app_icon.py <output.icns>")
    canvas = Canvas()
    # Circular chrome bezel with inset highlight.
    canvas.circle(512, 512, 328, (16, 25, 37, 255), width=29)
    canvas.circle(512, 512, 343, (154, 176, 196, 205), width=5)
    canvas.circle(512, 512, 312, (5, 11, 20, 255), width=7)

    # Faceted metallic helmet shell.
    helmet = [
        (512, 785), (438, 777), (367, 739), (329, 687), (311, 608),
        (337, 433), (362, 365), (409, 317), (512, 286), (615, 317),
        (662, 365), (687, 433), (713, 608), (695, 687), (657, 739),
        (586, 777),
    ]
    canvas.gradient_polygon(helmet, (178, 194, 209, 255), (57, 76, 96, 255))
    for index, point in enumerate(helmet):
        canvas.line(point, helmet[(index + 1) % len(helmet)], 11, (180, 198, 212, 255))
    canvas.line((415, 356), (473, 327), 6, (224, 236, 245, 220))
    canvas.line((551, 327), (609, 356), 6, (224, 236, 245, 220))

    # Dark face plate, with a luminous blue visor as the defining feature.
    face = [
        (512, 696), (452, 681), (411, 651), (386, 610), (397, 450),
        (424, 399), (465, 374), (512, 362), (559, 374), (600, 399),
        (627, 450), (638, 610), (613, 651), (572, 681),
    ]
    canvas.gradient_polygon(face, (21, 38, 57, 255), (3, 9, 17, 255))
    for index, point in enumerate(face):
        canvas.line(point, face[(index + 1) % len(face)], 6, (80, 105, 130, 255))

    # Side armor and illuminated visor.
    for side in (-1, 1):
        x = 512 + side * 201
        canvas.line((x, 460), (x + side * 13, 590), 18, (146, 167, 187, 245))
        canvas.circle(x + side * 13, 605, 13, (47, 83, 120, 255))
    canvas.line((393, 542), (631, 542), 74, (24, 109, 226, 100))
    canvas.line((398, 546), (626, 546), 42, (190, 226, 255, 255))
    canvas.line((410, 548), (614, 548), 17, (64, 144, 255, 255))
    canvas.line((456, 650), (568, 650), 10, (76, 105, 130, 220))

    canvas.save_icns(Path(sys.argv[1]))


if __name__ == "__main__":
    main()
