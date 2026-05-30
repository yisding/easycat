"""Property-based tests for interruption byte<->text/time estimation.

The interruption estimators in ``easycat.session.interruption`` map TTS
audio byte counts back to estimated spoken text and to bytes-likely-heard
at a cutoff time. These are pure heuristics, but they have hard
invariants the rest of the pipeline relies on: estimated text is always a
structural subset of the produced text (never invented), and heard-bytes
are always bounded by the bytes actually sent. Violations here would
corrupt the agent's committed conversation history on barge-in.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from easycat.session.interruption import (
    TtsChunk,
    _all_tts_audio_delivered,
    _audio_bytes_likely_heard,
    _estimate_text_spoken,
)

_TEXT = st.text(max_size=24)
_CHUNKS = st.lists(
    st.builds(
        TtsChunk,
        text=_TEXT,
        audio_bytes=st.integers(min_value=-50, max_value=5000),
        completed=st.booleans(),
    ),
    max_size=6,
)


@given(chunks=_CHUNKS, sent=st.integers(min_value=-100, max_value=20000))
def test_estimate_text_spoken_never_exceeds_total(chunks: list[TtsChunk], sent: int) -> None:
    spoken = _estimate_text_spoken(chunks, sent)
    total = "".join(chunk.text for chunk in chunks)
    # Never invents characters: the estimate is no longer than all the text.
    assert len(spoken) <= len(total)


@given(chunks=_CHUNKS)
def test_estimate_text_spoken_full_budget_returns_all_audible_text(
    chunks: list[TtsChunk],
) -> None:
    huge_budget = sum(max(chunk.audio_bytes, 0) for chunk in chunks) + 1
    spoken = _estimate_text_spoken(chunks, huge_budget)
    expected = "".join(chunk.text for chunk in chunks if chunk.audio_bytes > 0)
    assert spoken == expected


@given(chunks=_CHUNKS)
def test_estimate_text_spoken_zero_budget_is_empty(
    chunks: list[TtsChunk],
) -> None:
    assert _estimate_text_spoken(chunks, 0) == ""
    assert _estimate_text_spoken(chunks, -1) == ""


_SEND_LOG = st.lists(
    st.tuples(
        st.floats(min_value=0.0, max_value=10.0, allow_nan=False),
        st.integers(min_value=-20, max_value=2000),
        st.floats(min_value=0.0, max_value=500.0, allow_nan=False),
    ),
    max_size=8,
)


@given(
    send_log=_SEND_LOG,
    cutoff=st.one_of(st.none(), st.floats(min_value=-1.0, max_value=12.0, allow_nan=False)),
)
def test_audio_bytes_likely_heard_is_bounded(
    send_log: list[tuple[float, int, float]], cutoff: float | None
) -> None:
    total = sum(max(size, 0) for _, size, _ in send_log)
    heard = _audio_bytes_likely_heard(send_log, cutoff)
    # Heard bytes are non-negative and never exceed the bytes ever sent.
    assert 0 <= heard <= total


@given(send_log=_SEND_LOG, early=st.floats(min_value=0.0, max_value=5.0))
def test_audio_bytes_likely_heard_is_monotonic_in_cutoff(
    send_log: list[tuple[float, int, float]], early: float
) -> None:
    later = early + 100.0
    # Allowing more time can only let the user hear at least as much.
    assert _audio_bytes_likely_heard(send_log, early) <= _audio_bytes_likely_heard(send_log, later)


@given(chunks=_CHUNKS)
def test_all_delivered_implies_estimate_is_full_text(
    chunks: list[TtsChunk],
) -> None:
    total_audio = sum(max(chunk.audio_bytes, 0) for chunk in chunks)
    if not _all_tts_audio_delivered(chunks, total_audio):
        return
    spoken = _estimate_text_spoken(chunks, total_audio)
    expected = "".join(chunk.text for chunk in chunks if chunk.audio_bytes > 0)
    assert spoken == expected
