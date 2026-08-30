"""Stdlib-only PCM peak extraction and greyscale PNG waveform encoding.

The debugger renders a per-turn waveform strip both client-side (a Canvas
in ``static/waveform.js``) and server-side (a cheap ``<img>`` for the Live
view).  This module backs the server-side path: it downmixes raw PCM into a
fixed number of (min, max) peak buckets and hand-rolls a greyscale PNG with
clipped buckets tinted red.

Peak extraction buckets directly from the raw PCM sample array instead of
materialising every mono sample as a Python ``int``.  Unsupported formats
(notably 8-bit mu-law, ``sample_width == 1``) still yield silent buckets so the
caller can surface an "unsupported format" result rather than mis-decoded
garbage.

DEPENDENCY CONSTRAINT: ``numpy``/``Pillow`` are NOT available — the debugger
extra ships only ``aiohttp``.  Everything here is pure stdlib (``struct``,
``zlib``) so the endpoint works in a bare install.
"""

from __future__ import annotations

import struct
import sys
import zlib
from array import array

from easycat.debug._pcm import full_scale, is_supported_width

__all__ = [
    "decode_pcm_peaks",
    "encode_peaks_png",
    "full_scale",
    "is_supported_width",
]


def decode_pcm_peaks(
    pcm: bytes, *, sample_width: int, channels: int, buckets: int
) -> list[tuple[int, int]]:
    """Downmix ``pcm`` to ``buckets`` ``(min, max)`` integer peak pairs.

    Channels are averaged into mono before bucketing.  ``buckets`` is the
    exact length of the returned list; empty/short audio or an unsupported
    width (e.g. 8-bit mu-law) yields ``(0, 0)`` pairs so the caller can always
    paint a fixed-width strip.  Callers that must distinguish "unsupported"
    from "silence" should check :func:`easycat.debug._pcm.is_supported_width`
    first.
    """
    buckets = max(1, int(buckets))
    samples, frame_count, channels = _decode_pcm_sample_array(
        pcm, sample_width=sample_width, channels=channels
    )
    if frame_count == 0:
        return [(0, 0)] * buckets

    out: list[tuple[int, int]] = []
    for b in range(buckets):
        start = (b * frame_count) // buckets
        end = ((b + 1) * frame_count) // buckets
        if end <= start:
            end = min(start + 1, frame_count)
        lo: int | None = None
        hi: int | None = None
        for frame_index in range(start, end):
            sample_index = frame_index * channels
            if channels == 1:
                sample = samples[sample_index]
            else:
                total = 0
                for channel_offset in range(channels):
                    total += samples[sample_index + channel_offset]
                sample = total // channels
            lo = sample if lo is None else min(lo, sample)
            hi = sample if hi is None else max(hi, sample)
        out.append((lo, hi) if lo is not None and hi is not None else (0, 0))
    return out


def _decode_pcm_sample_array(
    pcm: bytes, *, sample_width: int, channels: int
) -> tuple[array[int], int, int]:
    """Return native samples plus complete frame count without Python-list expansion."""
    channels = max(1, int(channels))
    width = int(sample_width)
    if width == 2:
        typecode = "h"
    elif width == 4:
        typecode = "i"
    else:
        return array("h"), 0, channels

    frame_bytes = width * channels
    usable = (len(pcm) // frame_bytes) * frame_bytes
    if usable <= 0:
        return array(typecode), 0, channels

    samples = array(typecode)
    samples.frombytes(bytes(pcm[:usable]))
    if sys.byteorder == "big":  # pragma: no cover - rare BE host
        samples.byteswap()
    return samples, len(samples) // channels, channels


def encode_peaks_png(
    peaks: list[tuple[int, int]],
    *,
    width: int,
    height: int,
    full_scale_value: int | None = None,
    clipped_threshold: float = 0.99,
) -> bytes:
    """Hand-roll a greyscale waveform PNG (clipped columns tinted red).

    Each peak pair maps to one vertical column; the bar spans the column's
    min..max scaled to ``height``.  A column whose absolute peak reaches
    ``clipped_threshold`` of **full scale** is drawn red, the rest light grey
    on a dark background.  Output is a valid truecolour (RGB) PNG.

    ``full_scale_value`` is the format's real sample ceiling (e.g. 32767 for
    16-bit PCM, from :func:`easycat.debug._pcm.full_scale`).  The clip tint is
    keyed off it so only samples near the *format's* maximum are flagged —
    clean, quiet audio is never painted red just because it is the loudest
    column in the strip.  Vertical normalisation still uses the per-strip peak
    so quiet audio fills the height.  When ``full_scale_value`` is omitted the
    per-strip peak is used as a conservative fallback.
    """
    width = max(1, int(width))
    height = max(1, int(height))
    if not peaks:
        peaks = [(0, 0)]
    cols = len(peaks) or 1
    bg = (18, 22, 30)
    wave_rgb = (110, 168, 254)
    clip_rgb = (224, 99, 90)
    mid = height // 2
    # Per-strip peak: used ONLY for vertical normalisation so the bar fills the
    # available height even for quiet audio.
    strip_peak = 0
    for lo, hi in peaks:
        strip_peak = max(strip_peak, abs(lo), abs(hi))
    strip_peak = strip_peak or 1
    # Clip floor is keyed off the format's full scale, NOT the per-strip peak,
    # so only samples near the real ceiling are tinted.  Fall back to the strip
    # peak when the caller did not supply a full-scale reference.
    scale_ref = full_scale_value if full_scale_value and full_scale_value > 0 else strip_peak
    clip_floor = clipped_threshold * scale_ref
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # PNG filter byte (None) per scanline
        for x in range(width):
            lo, hi = peaks[(x * cols) // width]
            top = mid - round((hi / strip_peak) * mid)
            bot = mid - round((lo / strip_peak) * mid)
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
