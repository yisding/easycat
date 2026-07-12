"""Keep Chapter 8 threshold comparisons label-aware and controlled."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from easycat.debug.export import export_debug_bundle
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "08-smart-turn"
SWEEP = CHAPTER / "threshold_sweep.py"


def _bundle(tmp_path: Path) -> Path:
    journal = InMemoryRingBuffer(capacity=10)
    for probability in (0.2, 0.35, 0.45, 0.8):
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="smart_turn.classify",
            session_id="chapter-08-threshold",
            data={
                "stage": "turn",
                "probability": probability,
                "prediction": "complete" if probability >= 0.5 else "incomplete",
                "confirmed": probability >= 0.5,
            },
        )
    path = tmp_path / "chapter-08.bundle"
    export_debug_bundle(SimpleNamespace(journal=journal), path)
    return path


def test_unlabeled_sweep_reports_decision_changes_not_false_positives(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SWEEP), str(_bundle(tmp_path))],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(result.stdout)
    assert report["classification_count"] == 4
    assert report["newly_accepted_count"] == 2
    assert report["labeled_count"] == 0
    assert report["metrics"] is None
    assert [row["sequence"] for row in report["classifications"] if row["newly_accepted"]] == [
        2,
        3,
    ]


def test_labeled_sweep_distinguishes_helpful_acceptance_from_false_positive(
    tmp_path: Path,
) -> None:
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"1": False, "2": False, "3": True, "4": True}))
    result = subprocess.run(
        [sys.executable, str(SWEEP), str(_bundle(tmp_path)), "--labels", str(labels)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(result.stdout)
    assert report["labeled_count"] == 4
    assert report["metrics"] == {
        "baseline": {
            "false_negative": 1,
            "false_positive": 0,
            "true_negative": 2,
            "true_positive": 1,
        },
        "candidate": {
            "false_negative": 0,
            "false_positive": 1,
            "true_negative": 1,
            "true_positive": 2,
        },
    }


def test_threshold_lesson_requires_labels_before_naming_errors() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")

    assert "threshold_sweep.py" in readme
    assert "threshold_sweep.py" in exercises
    assert "new false-positives you bought" not in exercises
    assert "decision changes, not automatically" in readme
    assert "Without labels" in exercises
    assert "metrics` as `null`" in exercises
