"""Keep Chapter 15's manager exercise deterministic and provider-free."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.teaching import _script_runner as script_runner

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "15-operate-in-production"
PROBE = CHAPTER / "manager_probe.py"


def test_manager_probe_exercises_registry_and_failure_rollback() -> None:
    result = script_runner.run(
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
            "expected_error": "Failed to stop session sweep-failing: sweep-failing stop failed",
            "failed_slot_retained": True,
            "healthy_slot_released": True,
            "report": {
                "attempted_keys": ["sweep-healthy", "sweep-failing"],
                "failed_keys": ["sweep-failing"],
                "failures": [
                    {
                        "exception": "sweep-failing stop failed",
                        "key": "sweep-failing",
                    }
                ],
                "ok": False,
                "stopped_keys": ["sweep-healthy"],
            },
            "start_calls": {"sweep-failing": 1, "sweep-healthy": 1},
            "stop_calls": {"sweep-failing": 1, "sweep-healthy": 1},
        },
    }
    assert result.stderr == ""
