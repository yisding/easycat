"""Shared VAD state-machine (``_VADBase``) debounce tests."""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Literal

import pytest

from easycat.events import VADStartSpeaking, VADStopSpeaking
from easycat.vad._base import _VADBase

_LEAD_OFFSETS = range(0, 400, 7)
_RATE_16K = 16_000


def _frames_to_fire(
    *,
    frame_samples: int,
    sample_rate: int = _RATE_16K,
    min_ms: float,
    lead_frames: int,
    gate: Literal["start", "stop"],
) -> int:
    """Return how many gate frames it takes to emit the gate's event.

    ``lead_frames`` frames of the opposite state are consumed first so the gate
    starts at a different absolute stream position.
    """
    vad = _VADBase()
    vad.configure(min_speech_duration_ms=min_ms, min_silence_duration_ms=min_ms)
    if gate == "stop":
        vad._is_speaking = True
        vad._speech_confirmed = True
    lead_prob, gate_prob = (0.0, 1.0) if gate == "start" else (1.0, 0.0)
    expected = VADStartSpeaking if gate == "start" else VADStopSpeaking

    for _ in range(lead_frames):
        now = vad._advance_audio_time(frame_samples, sample_rate)
        assert list(vad._evaluate_speech(lead_prob, now)) == []

    for frames in range(1, 10_000):
        now = vad._advance_audio_time(frame_samples, sample_rate)
        events = list(vad._evaluate_speech(gate_prob, now))
        if events:
            assert [type(event) for event in events] == [expected]
            return frames
    raise AssertionError("gate never fired")


@pytest.mark.parametrize("gate", ["start", "stop"])
@pytest.mark.parametrize(
    ("frame_samples", "min_ms"),
    [
        (800, 250),  # FunASR default 50 ms chunk and min_speech_duration_ms
        (512, 256),  # Silero 512 samples (32 ms) @ 16 kHz
        (512, 320),
        (512, 64),
        (256, 160),  # TEN 256-sample (16 ms) hop @ 16 kHz
        (160, 250),  # Krisp 10 ms chunks
        (320, 150),  # Krisp 20 ms chunks, default min_silence_duration_ms
    ],
)
def test_vad_debounce_frame_count_is_independent_of_stream_position(
    frame_samples: int, min_ms: float, gate: Literal["start", "stop"]
) -> None:
    """Debounce must not depend on how much audio preceded the speech/silence.

    The consumed-audio clock used to be a running float sum of frame durations,
    so ``(now - start) * 1000`` could land just below a threshold that is an
    exact multiple of the frame duration (e.g. 249.99999999999997 < 250) and
    fire one frame late at some stream offsets only.
    """
    counts = {
        _frames_to_fire(frame_samples=frame_samples, min_ms=min_ms, lead_frames=lead, gate=gate)
        for lead in _LEAD_OFFSETS
    }

    # Start timestamp is the end of the first gate frame; fire when elapsed >= min.
    frame_ms = frame_samples * 1000 // _RATE_16K
    assert counts == {math.ceil(min_ms / frame_ms) + 1}


@pytest.mark.parametrize("gate", ["start", "stop"])
@pytest.mark.parametrize(
    ("frame_samples", "min_ms", "expected"),
    [
        (512, 0, 1),  # zero threshold still fires on the first frame
        (800, 260, 7),  # non-multiple threshold rounds up to the next frame
        (800, 249.5, 6),  # fractional thresholds are honoured
        (800, 250.5, 7),
    ],
)
def test_vad_debounce_edge_thresholds_are_position_independent(
    frame_samples: int, min_ms: float, expected: int, gate: Literal["start", "stop"]
) -> None:
    """Zero and non-multiple thresholds keep their nominal frame counts everywhere."""
    counts = {
        _frames_to_fire(frame_samples=frame_samples, min_ms=min_ms, lead_frames=lead, gate=gate)
        for lead in _LEAD_OFFSETS
    }

    assert counts == {expected}


@pytest.mark.parametrize("gate", ["start", "stop"])
@pytest.mark.parametrize(
    ("frame_samples", "sample_rate", "min_ms", "expected"),
    [
        # 7.8125 ms frames: 32 frames after the start frame are exactly 250 ms.
        # Rounding each frame to 7812 us used to fire one frame late (34th).
        (125, 16_000, 250, 33),
        # 3.333... ms frames: 48 frames after the start frame are exactly
        # 160 ms. Rounding each frame to 3333 us used to fire on the 50th frame.
        (160, 48_000, 160, 49),
    ],
)
def test_vad_debounce_is_exact_for_sub_microsecond_frame_durations(
    frame_samples: int,
    sample_rate: int,
    min_ms: float,
    expected: int,
    gate: Literal["start", "stop"],
) -> None:
    """Frame durations that are not whole microseconds must not drift the gate."""
    counts = {
        _frames_to_fire(
            frame_samples=frame_samples,
            sample_rate=sample_rate,
            min_ms=min_ms,
            lead_frames=lead,
            gate=gate,
        )
        for lead in _LEAD_OFFSETS
    }

    assert counts == {expected}


@pytest.mark.parametrize("gate", ["start", "stop"])
@pytest.mark.parametrize(
    ("frame_samples", "sample_rate", "min_ms"),
    [
        # 9.1875 ms frames round *up* to 9188 us, which used to fire one frame
        # early (1222 ms reached after 133 rounded frames vs 134 exact ones).
        (441, 48_000, 1222),
        # 11.609977... ms frames round up to 11610 us: early at 1161 ms.
        (512, 44_100, 1161),
        # 3.628117... ms frames round down to 3628 us: late at 439 ms.
        (160, 44_100, 439),
    ],
)
def test_vad_debounce_integer_ms_gate_is_exact_at_fractional_frame_duration(
    frame_samples: int, sample_rate: int, min_ms: int, gate: Literal["start", "stop"]
) -> None:
    """An integer-ms gate fires on the first frame whose exact elapsed time reaches it."""
    counts = {
        _frames_to_fire(
            frame_samples=frame_samples,
            sample_rate=sample_rate,
            min_ms=min_ms,
            lead_frames=lead,
            gate=gate,
        )
        for lead in _LEAD_OFFSETS
    }

    frames_after_start = math.ceil(Fraction(min_ms * sample_rate, 1000 * frame_samples))
    assert counts == {frames_after_start + 1}


def test_vad_audio_clock_tracks_consumed_seconds_and_resets() -> None:
    """The exact clock still exposes consumed audio in seconds and resets to zero."""
    vad = _VADBase()

    for _ in range(3000):
        now = vad._advance_audio_time(160, 48_000)

    assert now == 10
    assert vad._audio_time_s == 10.0

    vad.reset()

    assert vad._audio_time_s == 0.0
    assert vad._advance_audio_time(512, 16_000) == Fraction(32, 1000)
    assert vad._audio_time_s == 0.032
