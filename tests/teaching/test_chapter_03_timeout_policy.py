from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "docs" / "teaching" / "03-parrot-naive" / "timeout_policy_probe.py"


def test_timeout_policy_probe_exposes_the_fixed_timeout_tradeoff() -> None:
    completed = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["short_timeout"] == {
        "configured_timeout_ms": 500.0,
        "next_word_after_fire_ms": 45.0,
        "observed_silence_ms": 505.0,
        "outcome": "splits_before_next_word",
    }
    assert payload["long_timeout"] == {
        "configured_timeout_ms": 2000.0,
        "next_word_after_fire_ms": None,
        "observed_silence_ms": 2005.0,
        "outcome": "adds_commit_latency",
    }
    assert payload["one_timeout_cannot_optimize_both"] is True
