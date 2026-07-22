"""Shared stdlib-only PCM decode helpers for the debug/debugger audio paths.

Three near-identical decoders used to live in :mod:`easycat.debug._audio_health`
(``_iter_int16``), :mod:`easycat.debugger._aec` (``_pcm_to_samples``), and
:mod:`easycat.debugger._waveform` (its inline decode loop).  They all turned a
little-endian PCM blob into mono signed-integer samples, with subtly different
edge-case handling.  This module is the single source of truth they now call.

DEPENDENCY CONSTRAINT: ``numpy``/``audioop`` are NOT available — ``audioop`` was
removed from the stdlib in Python 3.13 and never carried mu-law helpers we could
rely on.  Everything here is pure stdlib (``array``, ``sys``).

SUPPORTED FORMATS: signed 16-bit (``sample_width == 2``) and signed 32-bit
(``sample_width == 4``) little-endian linear PCM, mono or interleaved multichannel
(channels are averaged to mono).  ``sample_width == 1`` is treated as UNSUPPORTED:
8-bit telephony audio is almost always mu-law companded, and decoding mu-law bytes
as linear int8 yields garbage.  Callers should branch on
:func:`is_supported_width` and surface an "unsupported format" result rather than
feeding 8-bit blobs through here.
"""

from __future__ import annotations

import sys
from array import array

# ``array`` typecodes for signed little-endian integer PCM keyed by sample width
# in bytes.  ``sample_width == 1`` is intentionally absent — 8-bit PCM is
# ambiguous (usually mu-law) and is reported as unsupported instead.
_WIDTH_TYPECODE = {2: "h", 4: "i"}


def is_supported_width(sample_width: int) -> bool:
    """True when *sample_width* is a linear PCM width this module can decode.

    Only 16-bit (2) and 32-bit (4) are supported.  8-bit (1) is rejected as
    ambiguous mu-law; any other width has no stdlib ``array`` typecode.
    """
    return int(sample_width) in _WIDTH_TYPECODE


def full_scale(sample_width: int) -> int:
    """Maximum positive sample magnitude for *sample_width* bytes.

    ``2**(8*width - 1) - 1`` — i.e. 32767 for 16-bit, 2147483647 for 32-bit.
    Used as the clipping/normalisation reference so "near full scale" is keyed
    off the format's real ceiling, not a per-strip maximum.
    """
    width = int(sample_width)
    if width <= 0:
        return 1
    return (1 << (8 * width - 1)) - 1


def decode_pcm_mono(blob: bytes, *, sample_width: int, channels: int) -> list[int]:
    """Decode *blob* to a flat mono list of signed ints (channels averaged).

    Returns an empty list for unsupported widths (``sample_width`` not in
    {2, 4}; notably 8-bit mu-law), empty/short input, or a non-positive channel
    count.  Interleaved frames are averaged across channels.  Decoding is
    byte-order normalised so big-endian hosts decode the same little-endian
    stream.  Trailing bytes that don't complete a frame are dropped.
    """
    typecode = _WIDTH_TYPECODE.get(int(sample_width))
    channels = max(1, int(channels))
    if typecode is None or not blob:
        return []
    samples = array(typecode)
    frame_bytes = int(sample_width) * channels
    usable = (len(blob) // frame_bytes) * frame_bytes
    if usable <= 0:
        return []
    samples.frombytes(bytes(blob[:usable]))
    # ``array`` is native-endian; normalise to little-endian on big-endian hosts
    # so detection/decoding is byte-order independent.
    if sys.byteorder == "big":  # pragma: no cover - rare BE host
        samples.byteswap()
    if channels == 1:
        return list(samples)
    return [sum(samples[i : i + channels]) // channels for i in range(0, len(samples), channels)]


__all__ = [
    "decode_pcm_mono",
    "full_scale",
    "is_supported_width",
]
