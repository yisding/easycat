"""Keep Chapter 15's manager exercise deterministic and provider-free."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "15-operate-in-production"
PROBE = CHAPTER / "manager_probe.py"


def test_manager_probe_exercises_registry_and_failure_rollback() -> None:
    result = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "active_together": True,
        "all_context_slots_released": True,
        "cancelled_start": {
            "cancelled_stop_calls": 0,
            "error": "CancelledError",
            "replacement_start_calls": 1,
            "replacement_stop_calls": 1,
            "replacement_used_released_slot": True,
            "slot_released": True,
        },
        "duplicate_key_error": "Session key already exists: alpha",
        "duplicate_start_calls": 0,
        "failed_slot_released": True,
        "failed_start_error": "failed start failed",
        "start_calls": {"alpha": 1, "beta": 1, "failed": 1},
        "stop_calls": {"alpha": 1, "beta": 1, "failed": 0},
        "stop_all": {
            "all_slots_released": True,
            "expected_error": "Failed to stop session sweep-failing: sweep-failing stop failed",
            "start_calls": {"sweep-failing": 1, "sweep-healthy": 1},
            "stop_calls": {"sweep-failing": 1, "sweep-healthy": 1},
        },
    }
    assert result.stderr == ""


def test_manager_exercise_does_not_invent_journal_events() -> None:
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    normalized = " ".join(exercises.split())

    assert "`SessionManager` has no journal" in exercises
    assert "not runtime record names" in normalized
    assert "manager_probe.py" in exercises
    assert "PortAudio device sharing varies" in normalized
    assert "`stop_all()` clears the registry" in exercises
    assert "does not prevent the other stop" in normalized
