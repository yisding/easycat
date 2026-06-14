"""Unit tests for the shared stdlib PCM decoder (FWP3).

``easycat.debug._pcm`` is the single decoder backing ``_audio_health``,
``debugger/_aec``, and ``debugger/_waveform``.  These exercise the decode of
int16/int32, stereo downmix, short/empty buffers, and the unsupported
8-bit/mu-law width.
"""

from __future__ import annotations

from array import array

from easycat.debug._pcm import (
    FULL_SCALE,
    decode_pcm_mono,
    full_scale,
    is_supported_width,
)


def test_full_scale_matches_format_ceiling():
    assert full_scale(2) == 32767
    assert full_scale(4) == 2147483647
    assert full_scale(1) == 127
    # ALL-CAPS alias points at the same callable.
    assert FULL_SCALE is full_scale


def test_full_scale_non_positive_width_is_one():
    assert full_scale(0) == 1
    assert full_scale(-3) == 1


def test_is_supported_width_only_16_and_32_bit():
    assert is_supported_width(2) is True
    assert is_supported_width(4) is True
    # 8-bit (mu-law) and odd widths are unsupported.
    assert is_supported_width(1) is False
    assert is_supported_width(3) is False
    assert is_supported_width(0) is False


def test_decode_int16_mono_round_trips():
    blob = array("h", [0, 1000, -2000, 32767, -32768]).tobytes()
    assert decode_pcm_mono(blob, sample_width=2, channels=1) == [0, 1000, -2000, 32767, -32768]


def test_decode_int32_mono_round_trips():
    values = [0, 70000, -70000, 2147483647, -2147483648]
    blob = array("i", values).tobytes()
    assert decode_pcm_mono(blob, sample_width=4, channels=1) == values


def test_decode_stereo_downmix_averages_channels():
    # Interleaved L/R: (100,300) -> 200, (-100,-300) -> -200.
    stereo = array("h", [100, 300, -100, -300]).tobytes()
    assert decode_pcm_mono(stereo, sample_width=2, channels=2) == [200, -200]


def test_decode_drops_trailing_partial_frame():
    # Two whole int16 samples plus one stray byte (half a sample) -> dropped.
    blob = array("h", [5, 6]).tobytes() + b"\x01"
    assert decode_pcm_mono(blob, sample_width=2, channels=1) == [5, 6]


def test_decode_empty_buffer_is_empty():
    assert decode_pcm_mono(b"", sample_width=2, channels=1) == []


def test_decode_buffer_shorter_than_one_frame_is_empty():
    # One byte cannot form a 16-bit sample.
    assert decode_pcm_mono(b"\x01", sample_width=2, channels=1) == []
    # Three bytes cannot form a complete stereo int16 frame (needs 4).
    assert decode_pcm_mono(b"\x01\x02\x03", sample_width=2, channels=2) == []


def test_decode_unsupported_width_yields_empty():
    # 8-bit mu-law (sample_width == 1) must NOT decode as linear int8 garbage.
    assert decode_pcm_mono(bytes(range(64)), sample_width=1, channels=1) == []
    # An unknown/odd width has no array typecode either.
    assert decode_pcm_mono(b"\x00\x00\x00", sample_width=3, channels=1) == []


def test_decode_non_positive_channels_clamped_to_mono():
    blob = array("h", [1, 2, 3]).tobytes()
    assert decode_pcm_mono(blob, sample_width=2, channels=0) == [1, 2, 3]
