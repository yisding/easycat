"""Keep Chapter 15's postmortem lifecycle proof executable and precise."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.teaching import _script_runner as script_runner

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "15-operate-in-production"


def test_postmortem_probe_preserves_view_records_and_bundle() -> None:
    completed = script_runner.run(
        [sys.executable, str(CHAPTER / "postmortem_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["response"] == "postmortem check"
    view = payload["journal_view"]
    records_before_stop = view["records_before_stop"]
    records_after_stop = view["records_after_stop"]
    assert view == {
        "type": "JournalView",
        "same_object_after_stop": True,
        "append_exposed_before_stop": False,
        "append_exposed_after_stop": False,
        "backend_before_stop": "SqliteJournal",
        "backend_after_stop": "ReadonlySqliteJournal",
        "records_before_stop": records_before_stop,
        "records_after_stop": records_after_stop,
        "records_preserved": True,
    }
    assert 0 < records_before_stop <= records_after_stop
    assert payload["bundle"] == {
        "exported_after_stop": True,
        "record_count": records_after_stop,
        "matches_postmortem_view": True,
    }
