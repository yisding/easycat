"""Keep Chapter 10's replay reference and signal metrics executable."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "10-cleaning-signal"


def test_replay_metrics_probe_enforces_reference_and_records_signal_change() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER / "replay_metrics_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "aligned": {
            "first_frame": {
                "cleaned_rms": 250.0,
                "frame_index": 1,
                "input_rms": 1000.0,
                "reference_fed": True,
                "stage": "audio",
                "vad_starts": 1,
            },
            "frame_records": 2,
            "summary": {
                "cleaned_rms": 250.0,
                "input_rms": 1000.0,
                "mic_frames": 2,
                "reference_frames_fed": 2,
                "rms_change_db": -12.041,
                "stage": "audio",
                "vad_starts": 1,
            },
        },
        "errors": {
            "missing_reference": "--ref is required when --aec on",
            "short_reference": "mic and ref frame counts differ for AEC: 2 vs 1",
        },
    }


def test_replay_source_records_promised_per_frame_and_summary_metrics() -> None:
    source = (CHAPTER / "replay.py").read_text(encoding="utf-8")

    assert 'name="replay.frame"' in source
    assert '"reference_fed": ref_fed' in source
    assert '"input_rms"' in source
    assert '"cleaned_rms"' in source
    assert '"rms_change_db"' in source
    assert 'raise SystemExit("--ref is required when --aec on")' in source


def test_lesson_treats_rms_as_signal_evidence_not_quality_score() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    lesson = " ".join(f"{readme}\n{exercises}".split())

    assert "replay_metrics_probe.py" in lesson
    assert "The replay fails closed" in lesson
    assert "RMS is not a quality score" in lesson
    assert "reference_frames_fed" in lesson
    assert "per-frame `replay.frame`" in lesson
