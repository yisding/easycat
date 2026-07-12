"""Keep Chapter 2's partial/final policy precise and executable."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "02-transcribe"
CHAPTER_3 = ROOT / "docs" / "teaching" / "03-parrot-naive"
CHAPTER_6 = ROOT / "docs" / "teaching" / "06-streaming-agent"


def _load_probe():
    path = CHAPTER / "partial_policy_probe.py"
    spec = importlib.util.spec_from_file_location("teaching_ch02_partial_policy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_revision_aware_policy_commits_only_the_final_hypothesis() -> None:
    probe = _load_probe()

    result = probe.compare_policies(probe.SCRIPTED_EVENTS)

    assert result == {
        "unsafe_irreversible_actions": [
            "set a timer for fifteen minutes",
            "set a timer for fifty minutes",
            "set a timer for fifty minutes",
        ],
        "ui_updates": [
            "set a timer for fifteen minutes",
            "set a timer for fifty minutes",
            "set a timer for fifty minutes",
        ],
        "speculations_started": [
            "set a timer for fifteen minutes",
            "set a timer for fifty minutes",
        ],
        "speculations_cancelled": ["set a timer for fifteen minutes"],
        "safe_commits": ["set a timer for fifty minutes"],
    }


def test_chapter_teaches_reversibility_instead_of_ignoring_partials() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    chapter_3 = (CHAPTER_3 / "README.md").read_text(encoding="utf-8")
    chapter_6 = (CHAPTER_6 / "README.md").read_text(encoding="utf-8")
    text = f"{readme}\n{exercises}\n{chapter_3}\n{chapter_6}".lower()

    assert "never act on a partial" not in text
    assert "never act on partials" not in text
    assert "never commit irreversible work from a partial" in text
    assert "cancellable speculation" in text
    assert "dispatch those from `final`, not `partial`" in text
    assert "irreversible output waits for\n`sttfinal`" in text
    assert "commit spoken output on final only" in text
