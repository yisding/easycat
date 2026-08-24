"""Shared G.711 mu-law codec helpers for telephony media transports."""

from __future__ import annotations

import struct

from easycat._audio_utils import resample

__all__ = [
    "_MULAW_BIAS",
    "_MULAW_CLIP",
    "_MULAW_DECODE_LUT",
    "_MULAW_ENCODE_LUT",
    "_mulaw_decode",
    "_mulaw_decode_sample",
    "_mulaw_encode",
    "_mulaw_encode_sample",
    "mulaw_to_pcm16",
    "pcm16_to_mulaw",
]


def mulaw_to_pcm16(mulaw_data: bytes, target_rate: int = 16000) -> bytes:
    """Convert mulaw 8 kHz audio to PCM16 at ``target_rate``."""
    pcm_8k = _mulaw_decode(mulaw_data)
    if target_rate == 8000:
        return pcm_8k
    return resample(pcm_8k, 8000, target_rate)


def pcm16_to_mulaw(pcm_data: bytes, source_rate: int = 16000) -> bytes:
    """Convert PCM16 at ``source_rate`` to mulaw 8 kHz."""
    if source_rate != 8000:
        pcm_data = resample(pcm_data, source_rate, 8000)
    return _mulaw_encode(pcm_data)


_MULAW_BIAS = 0x84
_MULAW_CLIP = 32635


def _mulaw_decode(mulaw_data: bytes) -> bytes:
    """Decode G.711 mu-law bytes into PCM16 little-endian bytes."""
    if not mulaw_data:
        return b""
    return struct.pack(f"<{len(mulaw_data)}h", *map(_MULAW_DECODE_LUT.__getitem__, mulaw_data))


def _mulaw_encode(pcm_data: bytes) -> bytes:
    """Encode PCM16 little-endian bytes into G.711 mu-law bytes."""
    if len(pcm_data) % 2 != 0:
        pcm_data = pcm_data[:-1]
    if not pcm_data:
        return b""
    count = len(pcm_data) // 2
    return bytes(map(_MULAW_ENCODE_LUT.__getitem__, struct.unpack(f"<{count}H", pcm_data)))


def _mulaw_decode_sample(value: int) -> int:
    """Decode a single mu-law byte into a signed PCM16 sample."""
    value = (~value) & 0xFF
    sign = value & 0x80
    exponent = (value >> 4) & 0x07
    mantissa = value & 0x0F
    sample = ((mantissa << 3) + _MULAW_BIAS) << exponent
    sample -= _MULAW_BIAS
    if sign:
        sample = -sample
    return sample


def _mulaw_encode_sample(sample: int) -> int:
    """Encode a signed PCM16 sample into a mu-law byte."""
    if sample < 0:
        sign = 0x80
        sample = -sample
    else:
        sign = 0x00

    sample = min(sample, _MULAW_CLIP)

    sample += _MULAW_BIAS
    exponent = 7
    exp_mask = 0x4000
    while exponent > 0 and (sample & exp_mask) == 0:
        exponent -= 1
        exp_mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


# Table-driven G.711 codec. The tables are precomputed from the reference
# per-sample formulas above, so table-driven output is byte-identical to the
# per-sample loops while avoiding per-sample Python work on the hot audio path.
_MULAW_DECODE_LUT: tuple[int, ...] = tuple(_mulaw_decode_sample(i) for i in range(256))
_MULAW_ENCODE_LUT: bytes = bytes(
    _mulaw_encode_sample(s if s < 32768 else s - 65536) for s in range(65536)
)
