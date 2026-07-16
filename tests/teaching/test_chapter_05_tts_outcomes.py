"""Keep Chapter 5's missing-first-audio diagnoses distinct."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "05-blocking-agent"


def test_tts_outcome_probe_exercises_three_distinct_causes() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER / "tts_outcome_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "all_rejected": {
            "accepted_chunks": 0,
            "gap_available": False,
            "outcome": "all_chunks_rejected",
            "rejected_chunks": 2,
            "turn_counts_match": True,
        },
        "mixed": {
            "accepted_chunks": 1,
            "gap_available": True,
            "outcome": "first_audio_accepted",
            "rejected_chunks": 1,
            "turn_counts_match": True,
        },
        "no_audio": {
            "accepted_chunks": 0,
            "gap_available": False,
            "outcome": "no_chunks_produced",
            "rejected_chunks": 0,
            "turn_counts_match": True,
        },
    }


def test_blocking_turn_preserves_counts_in_tts_and_gap_records() -> None:
    source = (CHAPTER / "main.py").read_text(encoding="utf-8")

    assert "accepted_chunks, rejected_chunks = await speak" in source
    assert "accepted_chunks=accepted_chunks" in source
    assert "rejected_chunks=rejected_chunks" in source
    assert '"tts_accepted_chunks": accepted_chunks' in source
    assert '"tts_rejected_chunks": rejected_chunks' in source
    assert "transport rejected all" in source
    assert "accepted TTS audio had no timestamp" in source


def test_lesson_separates_empty_tts_from_transport_rejection() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    lesson = " ".join(f"{readme}\n{exercises}".split())

    assert "A missing first-audio timestamp has multiple causes" in lesson
    assert "tts_outcome_probe.py" in lesson
    assert "all_chunks_rejected" in lesson
    assert "no_chunks_produced" in lesson
    assert "This is not a TTS-empty response" in lesson
    assert "scheduled for delivery, not rendered or heard" in lesson
