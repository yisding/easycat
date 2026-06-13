"""Stdlib-only PCM peak extraction and greyscale PNG waveform encoding.

The debugger renders a per-turn waveform strip both client-side (a Canvas
in ``static/waveform.js``) and server-side (a cheap ``<img>`` for the Live
view).  This module backs the server-side path: it downmixes raw PCM into a
fixed number of (min, max) peak buckets and hand-rolls a greyscale PNG with
clipped buckets tinted red.

DEPENDENCY CONSTRAINT: ``numpy``/``Pillow`` are NOT available — the debugger
extra ships only ``aiohttp``.  Everything here is pure stdlib (``array``,
``struct``, ``zlib``) so the endpoint works in a bare install.
"""

from __future__ import annotations

import struct
import zlib
from array import array

# ``array`` typecodes for signed integer PCM keyed by sample width in bytes.
_WIDTH_TYPECODE = {1: "b", 2: "h", 4: "i"}


def decode_pcm_peaks(
    pcm: bytes, *, sample_width: int, channels: int, buckets: int
) -> list[tuple[int, int]]:
    """Downmix ``pcm`` to ``buckets`` ``(min, max)`` integer peak pairs.

    Channels are averaged into mono before bucketing.  ``buckets`` is the
    exact length of the returned list; empty/short audio yields ``(0, 0)``
    pairs so the caller can always paint a fixed-width strip.
    """
    buckets = max(1, int(buckets))
    typecode = _WIDTH_TYPECODE.get(int(sample_width))
    channels = max(1, int(channels))
    if typecode is None or not pcm:
        return [(0, 0)] * buckets
    samples = array(typecode)
    frame_bytes = sample_width * channels
    usable = (len(pcm) // frame_bytes) * frame_bytes
    samples.frombytes(pcm[:usable])
    if struct.pack("=h", 1) != struct.pack("<h", 1):  # pragma: no cover - rare BE host
        samples.byteswap()
    # Downmix interleaved channels to mono frames by averaging.
    if channels > 1:
        mono = [
            sum(samples[i : i + channels]) // channels for i in range(0, len(samples), channels)
        ]
    else:
        mono = samples
    n = len(mono)
    if n == 0:
        return [(0, 0)] * buckets
    out: list[tuple[int, int]] = []
    for b in range(buckets):
        start = (b * n) // buckets
        end = ((b + 1) * n) // buckets
        if end <= start:
            end = min(start + 1, n)
        window = mono[start:end]
        out.append((min(window), max(window)) if window else (0, 0))
    return out


def encode_peaks_png(
    peaks: list[tuple[int, int]],
    *,
    width: int,
    height: int,
    clipped_threshold: float = 0.99,
) -> bytes:
    """Hand-roll a greyscale waveform PNG (clipped columns tinted red).

    Each peak pair maps to one vertical column; the bar spans the column's
    min..max scaled to ``height``.  A column whose absolute peak reaches
    ``clipped_threshold`` of full scale is drawn red, the rest light grey on a
    dark background.  Output is a valid truecolour (RGB) PNG.
    """
    width = max(1, int(width))
    height = max(1, int(height))
    cols = len(peaks) or 1
    bg = (18, 22, 30)
    wave_rgb = (110, 168, 254)
    clip_rgb = (224, 99, 90)
    mid = height // 2
    # Peaks are stored as signed ints in the source PCM's full-scale range;
    # normalise the largest possible magnitude across the strip to fill height.
    scale = 0
    for lo, hi in peaks:
        scale = max(scale, abs(lo), abs(hi))
    scale = scale or 1
    clip_floor = clipped_threshold * scale
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # PNG filter byte (None) per scanline
        for x in range(width):
            lo, hi = peaks[(x * cols) // width]
            top = mid - round((hi / scale) * mid)
            bot = mid - round((lo / scale) * mid)
            if y >= min(top, bot) and y <= max(top, bot):
                rgb = clip_rgb if max(abs(lo), abs(hi)) >= clip_floor else wave_rgb
            else:
                rgb = bg
            raw.extend(rgb)
    return _png_bytes(width, height, bytes(raw))


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    body = tag + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def _png_bytes(width: int, height: int, raw_scanlines: bytes) -> bytes:
    # 8-bit, colour type 2 (truecolour RGB), no interlace.
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", ihdr),
            _png_chunk(b"IDAT", zlib.compress(raw_scanlines, 9)),
            _png_chunk(b"IEND", b""),
        ]
    )
