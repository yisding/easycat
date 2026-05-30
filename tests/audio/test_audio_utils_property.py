"""Property-based tests for audio resampling and mono downmix.

These complement the example-based tests in ``test_audio_utils.py`` by
asserting structural invariants over randomized PCM16 input: same-rate
identity, output sized within int16 bounds, valid byte alignment, and
never-crash on odd trailing bytes. They exercise the pure-Python linear
backend directly so results are deterministic regardless of which
optional native backend (soxr/scipy) happens to be installed.
"""

from __future__ import annotations

import struct

from hypothesis import given
from hypothesis import strategies as st

import easycat._audio_utils as au
from easycat._audio_utils import resample, to_mono

_INT16 = st.integers(min_value=-32768, max_value=32767)
_RATES = st.sampled_from([8000, 16000, 22050, 24000, 44100, 48000])


def _pcm16(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


@given(samples=st.lists(_INT16, max_size=64), rate=_RATES)
def test_resample_same_rate_is_identity(samples: list[int], rate: int) -> None:
    data = _pcm16(samples)
    assert resample(data, rate, rate) == data


@given(
    samples=st.lists(_INT16, max_size=64),
    from_rate=_RATES,
    to_rate=_RATES,
)
def test_resample_linear_output_is_aligned_int16(
    samples: list[int], from_rate: int, to_rate: int
) -> None:
    out = au._resample_linear(_pcm16(samples), from_rate, to_rate)
    # Output is whole PCM16 frames and every decoded sample is in range.
    assert len(out) % 2 == 0
    decoded = struct.unpack(f"<{len(out) // 2}h", out)
    assert all(-32768 <= value <= 32767 for value in decoded)


@given(
    data=st.binary(max_size=129),
    from_rate=_RATES,
    to_rate=_RATES,
)
def test_resample_never_crashes_on_arbitrary_bytes(
    data: bytes, from_rate: int, to_rate: int
) -> None:
    # Arbitrary byte length (including odd trailing byte) must not raise.
    out = resample(data, from_rate, to_rate)
    assert isinstance(out, bytes)
    # Same-rate is verbatim pass-through (may keep an odd trailing byte);
    # only the actual resampling path guarantees whole PCM16 frames.
    if from_rate != to_rate:
        assert len(out) % 2 == 0


@given(
    frames=st.lists(st.tuples(_INT16, _INT16), max_size=64),
)
def test_to_mono_stereo_preserves_frame_count_and_range(
    frames: list[tuple[int, int]],
) -> None:
    flat: list[int] = [sample for frame in frames for sample in frame]
    mono = to_mono(_pcm16(flat), channels=2)
    decoded = struct.unpack(f"<{len(mono) // 2}h", mono)
    # One mono sample per input frame, each within the averaged-pair range.
    assert len(decoded) == len(frames)
    for (left, right), avg in zip(frames, decoded, strict=True):
        assert min(left, right) <= avg <= max(left, right)


@given(samples=st.lists(_INT16, max_size=64))
def test_to_mono_mono_is_identity(samples: list[int]) -> None:
    data = _pcm16(samples)
    assert to_mono(data, channels=1) == data
