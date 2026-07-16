"""Keep Chapter 15's postmortem lifecycle proof executable and precise."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "15-operate-in-production"


def test_postmortem_probe_preserves_view_records_and_bundle() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER / "postmortem_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["response"] == "postmortem check"
    view = payload["journal_view"]
    record_count = view["records_after_stop"]
    assert view == {
        "type": "JournalView",
        "same_object_after_stop": True,
        "append_exposed_before_stop": False,
        "append_exposed_after_stop": False,
        "backend_before_stop": "SqliteJournal",
        "backend_after_stop": "ReadonlySqliteJournal",
        "records_before_stop": record_count,
        "records_after_stop": record_count,
        "records_preserved": True,
    }
    assert record_count > 0
    assert payload["bundle"] == {
        "exported_after_stop": True,
        "record_count": record_count,
        "matches_postmortem_view": True,
    }


def test_chapter_teaches_journal_view_is_always_read_only() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    normalized = " ".join(f"{readme}\n{exercises}".split())

    assert "postmortem_probe.py" in normalized
    assert "stable, read-only `JournalView`" in normalized
    assert "application code never appends through it" in normalized
    assert "`append_exposed_before_stop` was already false" in normalized
    assert "`InMemoryRingBuffer` to `FrozenJournalSnapshot`" in normalized
