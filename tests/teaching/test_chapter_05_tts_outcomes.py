"""Keep Chapter 5's missing-first-audio diagnoses distinct."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.teaching import _script_runner as script_runner

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "05-blocking-agent"


def test_tts_outcome_probe_exercises_three_distinct_causes() -> None:
    completed = script_runner.run(
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
