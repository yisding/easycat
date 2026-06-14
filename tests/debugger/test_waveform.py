from __future__ import annotations

import struct
import zlib
from array import array

from easycat.debug._pcm import full_scale, is_supported_width
from easycat.debugger._waveform import decode_pcm_peaks, encode_peaks_png

_CLIP_RGB = bytes((224, 99, 90))
_WAVE_RGB = bytes((110, 168, 254))


def _scanline_pixel(raw: bytes, *, x: int, y: int, width: int) -> bytes:
    """Extract the RGB triple at column ``x`` / row ``y`` from raw scanlines."""
    row_stride = 1 + width * 3
    base = y * row_stride + 1 + x * 3
    return raw[base : base + 3]


def _column_has_colour(raw: bytes, *, x: int, height: int, width: int, colour: bytes) -> bool:
    return any(_scanline_pixel(raw, x=x, y=y, width=width) == colour for y in range(height))


def _parse_png_chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    """Walk a PNG byte stream, validating every chunk's CRC.

    Returns ``[(tag, payload), ...]``; raises ``AssertionError`` on a bad
    signature or CRC so the test catches a malformed hand-rolled encoder.
    """
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "missing PNG signature"
    chunks: list[tuple[bytes, bytes]] = []
    idx = 8
    while idx < len(png):
        length = struct.unpack(">I", png[idx : idx + 4])[0]
        tag = png[idx + 4 : idx + 8]
        payload = png[idx + 8 : idx + 8 + length]
        crc = struct.unpack(">I", png[idx + 8 + length : idx + 12 + length])[0]
        assert crc == zlib.crc32(png[idx + 4 : idx + 8 + length]), f"bad CRC for {tag!r}"
        chunks.append((tag, payload))
        idx += 12 + length
    return chunks


def test_decode_pcm_peaks_returns_exact_bucket_count_in_range():
    pcm = array("h", [0, 1000, -2000, 32767, -32768, 5000, -5000, 100]).tobytes()
    peaks = decode_pcm_peaks(pcm, sample_width=2, channels=1, buckets=4)
    assert len(peaks) == 4
    for lo, hi in peaks:
        assert -32768 <= lo <= hi <= 32767


def test_decode_pcm_peaks_pads_short_or_empty_audio():
    assert decode_pcm_peaks(b"", sample_width=2, channels=1, buckets=5) == [(0, 0)] * 5
    # Fewer samples than buckets still yields exactly ``buckets`` pairs.
    short = array("h", [7, -7]).tobytes()
    peaks = decode_pcm_peaks(short, sample_width=2, channels=1, buckets=6)
    assert len(peaks) == 6


def test_decode_pcm_peaks_downmixes_stereo_by_averaging():
    # Interleaved L/R: (100,300) -> 200, (-100,-300) -> -200.
    stereo = array("h", [100, 300, -100, -300]).tobytes()
    peaks = decode_pcm_peaks(stereo, sample_width=2, channels=2, buckets=2)
    assert peaks == [(200, 200), (-200, -200)]


def test_decode_pcm_peaks_unknown_width_yields_silence():
    peaks = decode_pcm_peaks(b"\x00\x00\x00", sample_width=3, channels=1, buckets=3)
    assert peaks == [(0, 0)] * 3


def test_encode_peaks_png_emits_valid_truecolour_png():
    peaks = decode_pcm_peaks(
        array("h", [0, 1000, -1000, 32000] * 8).tobytes(),
        sample_width=2,
        channels=1,
        buckets=30,
    )
    png = encode_peaks_png(peaks, width=60, height=24)
    chunks = _parse_png_chunks(png)
    tags = [tag for tag, _ in chunks]
    assert tags == [b"IHDR", b"IDAT", b"IEND"]
    ihdr = chunks[0][1]
    width, height, bit_depth, colour_type = struct.unpack(">IIBB", ihdr[:10])
    assert (width, height, bit_depth, colour_type) == (60, 24, 8, 2)
    # IDAT must inflate to exactly height * (1 filter byte + width*3 RGB).
    raw = zlib.decompress(chunks[1][1])
    assert len(raw) == height * (1 + width * 3)


def test_encode_peaks_png_marks_clipped_columns_red():
    # One non-clipped column, one full-scale (clipped) column.
    peaks = [(0, 1000), (-32768, 32767)]
    png = encode_peaks_png(
        peaks,
        width=2,
        height=4,
        full_scale_value=full_scale(2),
        clipped_threshold=0.99,
    )
    raw = zlib.decompress(_parse_png_chunks(png)[1][1])
    # The clipped column maps to x=1; the quiet column (x=0) must stay non-red.
    assert _column_has_colour(raw, x=1, height=4, width=2, colour=_CLIP_RGB), (
        "near-full-scale column should contain red pixels"
    )
    assert not _column_has_colour(raw, x=0, height=4, width=2, colour=_CLIP_RGB), (
        "quiet column must not be tinted red"
    )


def test_encode_peaks_png_clean_audio_has_no_red_columns():
    # Clean, well-below-full-scale audio: the loudest column is far from the
    # format ceiling, so NO column should be painted red.  This is the bug
    # the full-scale clip floor fixes — the old per-strip-max floor always
    # tinted the loudest column even on clean audio.
    pcm = array("h", [int(4000 * (1 if i % 2 else -1)) for i in range(64)]).tobytes()
    peaks = decode_pcm_peaks(pcm, sample_width=2, channels=1, buckets=16)
    png = encode_peaks_png(peaks, width=32, height=16, full_scale_value=full_scale(2))
    raw = zlib.decompress(_parse_png_chunks(png)[1][1])
    for x in range(32):
        assert not _column_has_colour(raw, x=x, height=16, width=32, colour=_CLIP_RGB), (
            f"clean audio column {x} must not be tinted red"
        )
    # ...but the waveform itself is still drawn (light-grey wave pixels present).
    assert any(
        _column_has_colour(raw, x=x, height=16, width=32, colour=_WAVE_RGB) for x in range(32)
    ), "clean audio should still render a (non-red) waveform"


def test_encode_peaks_png_only_near_full_scale_is_tinted():
    # A loud-but-not-clipping column (90% of full scale) next to a true
    # full-scale column: only the latter is tinted at the default 0.99 floor.
    near = int(0.90 * full_scale(2))
    peaks = [(-near, near), (-32768, 32767)]
    png = encode_peaks_png(peaks, width=2, height=8, full_scale_value=full_scale(2))
    raw = zlib.decompress(_parse_png_chunks(png)[1][1])
    assert not _column_has_colour(raw, x=0, height=8, width=2, colour=_CLIP_RGB), (
        "90%-of-full-scale column must not be tinted"
    )
    assert _column_has_colour(raw, x=1, height=8, width=2, colour=_CLIP_RGB), (
        "full-scale column must be tinted"
    )


def test_decode_pcm_peaks_mulaw_width_one_is_unsupported():
    # 8-bit / mu-law telephony audio (sample_width == 1) is unsupported: it must
    # NOT be decoded as linear int8 garbage.  The shared support check reports
    # it, and decoding yields only silence pads (no spurious peaks).
    assert is_supported_width(1) is False
    assert is_supported_width(2) is True
    blob = bytes(range(0, 256)) * 4  # arbitrary mu-law-ish bytes
    peaks = decode_pcm_peaks(blob, sample_width=1, channels=1, buckets=8)
    assert peaks == [(0, 0)] * 8


def test_encode_peaks_png_handles_silence_without_crashing():
    png = encode_peaks_png([(0, 0)] * 10, width=10, height=8, full_scale_value=full_scale(2))
    assert _parse_png_chunks(png)[0][0] == b"IHDR"
