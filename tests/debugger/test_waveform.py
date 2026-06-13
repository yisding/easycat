from __future__ import annotations

import struct
import zlib
from array import array

from easycat.debugger._waveform import decode_pcm_peaks, encode_peaks_png


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
    png = encode_peaks_png(peaks, width=2, height=4, clipped_threshold=0.99)
    raw = zlib.decompress(_parse_png_chunks(png)[1][1])
    clip_rgb = bytes((224, 99, 90))
    # The clipped column maps to x=1; scan every scanline's second pixel.
    row_stride = 1 + 2 * 3
    found_red = False
    for y in range(4):
        px1 = raw[y * row_stride + 1 + 3 : y * row_stride + 1 + 6]
        if px1 == clip_rgb:
            found_red = True
    assert found_red, "clipped column should contain red pixels"


def test_encode_peaks_png_handles_silence_without_crashing():
    png = encode_peaks_png([(0, 0)] * 10, width=10, height=8)
    assert _parse_png_chunks(png)[0][0] == b"IHDR"
