"""Unit tests for mulaw encode/decode roundtrip."""

from __future__ import annotations

import struct

from easycat.transports import twilio_media
from easycat.transports.twilio_media import (
    _mulaw_decode_sample,
    _mulaw_encode_sample,
    mulaw_to_pcm16,
    pcm16_to_mulaw,
)


class TestMulawRoundtrip:
    """Test that mulaw encode->decode preserves audio within tolerance."""

    def test_silence_roundtrip(self) -> None:
        pcm = bytes(320)  # 160 samples of silence
        mulaw = pcm16_to_mulaw(pcm, source_rate=8000)
        restored = mulaw_to_pcm16(mulaw, target_rate=8000)
        samples = struct.unpack(f"<{len(restored) // 2}h", restored)
        assert all(abs(s) < 10 for s in samples)

    def test_max_positive_sample(self) -> None:
        encoded = _mulaw_encode_sample(32767)
        decoded = _mulaw_decode_sample(encoded)
        assert decoded > 30000
        assert decoded <= 32767

    def test_max_negative_sample(self) -> None:
        encoded = _mulaw_encode_sample(-32768)
        decoded = _mulaw_decode_sample(encoded)
        assert decoded < -30000
        assert decoded >= -32768

    def test_zero_sample(self) -> None:
        encoded = _mulaw_encode_sample(0)
        decoded = _mulaw_decode_sample(encoded)
        assert abs(decoded) < 200  # mulaw bias means 0 doesn't map exactly

    def test_all_256_mulaw_values_decode(self) -> None:
        """Every possible mulaw byte should decode without error."""
        for i in range(256):
            sample = _mulaw_decode_sample(i)
            assert -32768 <= sample <= 32767

    def test_resample_8k_to_16k(self) -> None:
        pcm_8k = bytes(320)  # 160 samples at 8kHz
        pcm_16k = mulaw_to_pcm16(pcm16_to_mulaw(pcm_8k, source_rate=8000), target_rate=16000)
        assert len(pcm_16k) > 300  # roughly double
        assert len(pcm_16k) < 700

    def test_odd_length_pcm_data(self) -> None:
        """Odd-length PCM data should be handled (truncate last byte)."""
        pcm = bytes(321)
        mulaw = pcm16_to_mulaw(pcm, source_rate=8000)
        assert len(mulaw) == 160  # 320 bytes -> 160 mulaw samples

    def test_empty_data(self) -> None:
        mulaw = pcm16_to_mulaw(b"", source_rate=8000)
        assert mulaw == b""
        pcm = mulaw_to_pcm16(b"", target_rate=8000)
        assert pcm == b""


class TestMulawTableParity:
    """Pin the table-driven codec byte-for-byte against the reference formulas."""

    def test_decode_matches_reference_for_all_bytes(self) -> None:
        """Every mulaw byte decodes to the reference per-sample value."""
        decoded = mulaw_to_pcm16(bytes(range(256)), target_rate=8000)
        assert struct.unpack("<256h", decoded) == tuple(
            _mulaw_decode_sample(i) for i in range(256)
        )

    def test_codec_is_table_driven(self) -> None:
        """The hot path uses precomputed lookup tables (not per-sample loops)."""
        assert len(twilio_media._MULAW_DECODE_LUT) == 256
        assert len(twilio_media._MULAW_ENCODE_LUT) == 65536

    def test_encode_matches_reference_for_all_int16(self) -> None:
        """Every int16 PCM sample encodes to the reference mulaw byte."""
        samples = range(-32768, 32768)
        pcm = struct.pack(f"<{len(samples)}h", *samples)
        encoded = pcm16_to_mulaw(pcm, source_rate=8000)
        assert encoded == bytes(_mulaw_encode_sample(s) for s in samples)
