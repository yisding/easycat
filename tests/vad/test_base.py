"""Shared VAD state-machine (``_VADBase``) debounce tests."""

from __future__ import annotations

import math
from typing import Literal

import pytest

from easycat.events import VADStartSpeaking, VADStopSpeaking
from easycat.vad._base import _VADBase

_LEAD_OFFSETS = range(0, 400, 7)


def _frames_to_fire(
    *,
    frame_ms: float,
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
        now = vad._advance_audio_time(frame_ms / 1000)
        assert list(vad._evaluate_speech(lead_prob, now)) == []

    for frames in range(1, 10_000):
        now = vad._advance_audio_time(frame_ms / 1000)
        events = list(vad._evaluate_speech(gate_prob, now))
        if events:
            assert [type(event) for event in events] == [expected]
            return frames
    raise AssertionError("gate never fired")


@pytest.mark.parametrize("gate", ["start", "stop"])
@pytest.mark.parametrize(
    ("frame_ms", "min_ms"),
    [
        (50, 250),  # FunASR default chunk and min_speech_duration_ms
        (32, 256),  # Silero 512 samples @ 16 kHz
        (32, 320),
        (32, 64),
        (16, 160),  # TEN 256-sample hop @ 16 kHz
        (10, 250),  # Krisp 10 ms chunks
        (20, 150),  # Krisp 20 ms chunks, default min_silence_duration_ms
    ],
)
def test_vad_debounce_frame_count_is_independent_of_stream_position(
    frame_ms: float, min_ms: float, gate: Literal["start", "stop"]
) -> None:
    """Debounce must not depend on how much audio preceded the speech/silence.

    The consumed-audio clock used to be a running float sum of frame durations,
    so ``(now - start) * 1000`` could land just below a threshold that is an
    exact multiple of the frame duration (e.g. 249.99999999999997 < 250) and
    fire one frame late at some stream offsets only.
    """
    counts = {
        _frames_to_fire(frame_ms=frame_ms, min_ms=min_ms, lead_frames=lead, gate=gate)
        for lead in _LEAD_OFFSETS
    }

    # Start timestamp is the end of the first gate frame; fire when elapsed >= min.
    assert counts == {math.ceil(min_ms / frame_ms) + 1}


@pytest.mark.parametrize("gate", ["start", "stop"])
@pytest.mark.parametrize(
    ("frame_ms", "min_ms", "expected"),
    [
        (32, 0, 1),  # zero threshold still fires on the first frame
        (50, 260, 7),  # non-multiple threshold rounds up to the next frame
        (50, 249.5, 6),  # fractional thresholds are honoured
        (50, 250.5, 7),
    ],
)
def test_vad_debounce_edge_thresholds_are_position_independent(
    frame_ms: float, min_ms: float, expected: int, gate: Literal["start", "stop"]
) -> None:
    """Zero and non-multiple thresholds keep their nominal frame counts everywhere."""
    counts = {
        _frames_to_fire(frame_ms=frame_ms, min_ms=min_ms, lead_frames=lead, gate=gate)
        for lead in _LEAD_OFFSETS
    }

    assert counts == {expected}


def test_vad_audio_clock_tracks_consumed_seconds_and_resets() -> None:
    """The exact clock still exposes consumed audio in seconds and resets to zero."""
    vad = _VADBase()

    for _ in range(1000):
        now = vad._advance_audio_time(0.01)

    assert now == 10.0
    assert vad._audio_time_s == 10.0

    vad.reset()

    assert vad._audio_time_s == 0.0
    assert vad._advance_audio_time(0.032) == 0.032
