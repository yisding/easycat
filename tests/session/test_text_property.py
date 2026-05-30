"""Property-based tests for Session text helpers.

Covers the pure text utilities in ``easycat.session.text``: streaming
sentence-boundary splitting (lossless reconstruction), partial-text
boundary truncation (length bounds + prefix property), markdown
delimiter balance (never-crash), and the PCM16 speech-energy gate
(never-crash + threshold monotonicity).
"""

from __future__ import annotations

import struct

from hypothesis import given
from hypothesis import strategies as st

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.session.text import (
    _chunk_has_speech_energy,
    _truncate_partial_text_to_boundary,
    has_unclosed_markdown_delimiters,
    split_at_sentence_boundaries,
)

_INT16 = st.integers(min_value=-32768, max_value=32767)


@given(text=st.text(max_size=200))
def test_split_at_sentence_boundaries_is_lossless(text: str) -> None:
    ready, remaining = split_at_sentence_boundaries(text)
    # The split partitions the input exactly: no text lost or duplicated.
    assert ready + remaining == text


@given(text=st.text(max_size=80), chars=st.integers(min_value=-5, max_value=120))
def test_truncate_partial_text_is_bounded_prefix(text: str, chars: int) -> None:
    out = _truncate_partial_text_to_boundary(text, chars)
    # Result is always a prefix of the input, never longer than it.
    assert text.startswith(out)
    assert len(out) <= len(text)
    if chars <= 0:
        assert out == ""
    if chars >= len(text):
        assert out == text


@given(text=st.text(max_size=120))
def test_has_unclosed_markdown_returns_bool_and_never_crashes(text: str) -> None:
    assert isinstance(has_unclosed_markdown_delimiters(text), bool)


@given(text=st.text(alphabet=st.sampled_from(list("ab `*_~[]()\\")), max_size=40))
def test_has_unclosed_markdown_stable_on_delimiter_soup(text: str) -> None:
    # Focused on delimiter characters: must still return a bool, not raise.
    assert isinstance(has_unclosed_markdown_delimiters(text), bool)


@given(samples=st.lists(_INT16, max_size=200))
def test_chunk_has_speech_energy_is_bool_and_threshold_monotonic(
    samples: list[int],
) -> None:
    data = struct.pack(f"<{len(samples)}h", *samples)
    chunk = AudioChunk(data=data, format=PCM16_MONO_16K)
    loud = _chunk_has_speech_energy(chunk, threshold=1)
    quiet = _chunk_has_speech_energy(chunk, threshold=40000)
    assert isinstance(loud, bool)
    # A lower threshold can only make the gate more (or equally) permissive.
    assert loud or not quiet


@given(samples=st.lists(_INT16, max_size=200), threshold=st.integers(min_value=1, max_value=40000))
def test_chunk_has_speech_energy_matches_reference_peak(
    samples: list[int],
    threshold: int,
) -> None:
    # The batch decode must agree with a straightforward reference scan,
    # including the abs(-32768) widening edge case.
    data = struct.pack(f"<{len(samples)}h", *samples)
    chunk = AudioChunk(data=data, format=PCM16_MONO_16K)
    expected = bool(samples) and max(abs(s) for s in samples) >= threshold
    assert _chunk_has_speech_energy(chunk, threshold=threshold) is expected


def test_chunk_has_speech_energy_handles_min_int16_and_odd_byte() -> None:
    # abs(-32768) must not wrap negative under the numpy path.
    chunk = AudioChunk(data=struct.pack("<h", -32768), format=PCM16_MONO_16K)
    assert _chunk_has_speech_energy(chunk, threshold=32768) is True
    assert _chunk_has_speech_energy(chunk, threshold=32769) is False
    # An odd trailing byte is dropped rather than crashing struct/numpy.
    odd = struct.pack("<h", 1000) + b"\x7f"
    odd_chunk = AudioChunk(data=odd, format=PCM16_MONO_16K)
    assert _chunk_has_speech_energy(odd_chunk, threshold=500) is True
    assert _chunk_has_speech_energy(odd_chunk, threshold=2000) is False
