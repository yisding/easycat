"""Keep Chapter 11's empty-query evidence explicit and automatable."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "11-journal"
INVESTIGATE = CHAPTER / "investigate.py"
BUNDLE = CHAPTER / "bundles" / "bug_03_ghost_interruption.bundle"


def run_investigate(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(INVESTIGATE), str(BUNDLE), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_query_coverage_probe_distinguishes_typo_and_intersection() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER / "query_coverage_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {
        "impossible_intersection": {
            "combined_matches": 0,
            "marginal_matches": {"sequence": 1, "stage": 2},
        },
        "total_records": 13,
        "turn_typo": {
            "known_turns": ["ch11-bug03-turn-1", "ch11-bug03-turn-2"],
            "marginal_matches": {"turn": 0},
        },
        "valid_combined_query": [9],
    }


def test_empty_query_reports_filter_coverage_and_known_values() -> None:
    completed = run_investigate("--turn", "typo")

    assert completed.returncode == 0
    assert "filters: turn='typo'" in completed.stdout
    assert "matched: 0 of 13 records" in completed.stdout
    assert "marginal turn: 0 matches" in completed.stdout
    assert "known turns: ['ch11-bug03-turn-1', 'ch11-bug03-turn-2']" in completed.stdout


def test_require_match_fails_for_automation_without_reclassifying_absence() -> None:
    completed = run_investigate("--turn", "typo", "--require-match")

    assert completed.returncode == 1
    assert "(no records matched)" in completed.stdout


def test_limit_is_positive_and_only_reports_real_truncation() -> None:
    invalid = run_investigate("--limit", "0")
    assert invalid.returncode == 2
    assert "must be a positive integer" in invalid.stderr

    exact = run_investigate("--sequence", "9", "--limit", "1")
    assert exact.returncode == 0
    assert "matched: 1 of 13 records" in exact.stdout
    assert "showing 1 of" not in exact.stdout

    truncated = run_investigate("--limit", "1")
    assert truncated.returncode == 0
    assert "... (showing 1 of 13 matches)" in truncated.stdout


def test_chapter_teaches_query_coverage_before_absence() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    normalized = " ".join(f"{readme}\n{exercises}".split())

    assert "Validate an empty query" in readme
    assert "query_coverage_probe.py" in readme
    assert "--require-match" in readme
    assert "query_coverage_probe.py" in exercises
    assert "filtered sequence gap" in normalized
    assert "stt.final" in exercises and "agent.first_token" in exercises
