"""Keep Chapter 11's turn and session query scopes explicit."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "11-journal"
INVESTIGATE = CHAPTER / "investigate.py"
BUNDLE = CHAPTER / "bundles" / "bug_03_ghost_interruption.bundle"


def load_investigator():
    spec = importlib.util.spec_from_file_location("teaching_ch11_session_context", INVESTIGATE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_session_context_probe_recovers_ghost_interruption_configuration() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER / "session_context_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "audio_context": [
            {
                "aec": "off",
                "name": "audio.config",
                "sequence": 1,
                "session_id": "ch11-bug03",
                "turn_id": None,
            }
        ],
        "strict_turn_sequences": [8, 9, 10, 11, 12, 13],
        "with_context_sequences": [1, 8, 9, 10, 11, 12, 13],
    }


def test_context_join_does_not_mix_unscoped_records_from_other_sessions() -> None:
    investigator = load_investigator()
    records = [
        {"sequence": 1, "session_id": "a", "turn_id": None, "name": "config", "data": {}},
        {"sequence": 2, "session_id": "a", "turn_id": "target", "name": "turn", "data": {}},
        {"sequence": 3, "session_id": "b", "turn_id": None, "name": "config", "data": {}},
        {"sequence": 4, "session_id": "b", "turn_id": "other", "name": "turn", "data": {}},
        {"sequence": 5, "session_id": None, "turn_id": None, "name": "config", "data": {}},
        {"sequence": 6, "session_id": None, "turn_id": "target", "name": "turn", "data": {}},
    ]

    class Bundle:
        def records(self):
            return iter(records)

        def filter_by_turn(self, turn):
            return [record for record in records if record["turn_id"] == turn]

        def filter_by_stage(self, _stage):
            return []

        def lookup_by_sequence(self, sequence):
            return next((record for record in records if record["sequence"] == sequence), None)

    matches = investigator.query_records(Bundle(), turn="target", include_session_context=True)
    assert [record["sequence"] for record in matches] == [1, 2, 6]


def test_cli_requires_turn_and_reports_context_coverage() -> None:
    invalid = subprocess.run(
        [sys.executable, str(INVESTIGATE), str(BUNDLE), "--include-session-context"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert invalid.returncode == 2
    assert "--include-session-context requires --turn" in invalid.stderr

    completed = subprocess.run(
        [
            sys.executable,
            str(INVESTIGATE),
            str(BUNDLE),
            "--turn",
            "ch11-bug03-turn-2",
            "--stage",
            "audio",
            "--include-session-context",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "matched: 1 of 13 records" in completed.stdout
    assert "session context: 1 unscoped records included from target session" in completed.stdout
    assert "audio.config" in completed.stdout


def test_lesson_teaches_turn_isolation_and_session_context_as_distinct_scopes() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    lesson = " ".join(f"{readme}\n{exercises}".split())

    assert "session_context_probe.py" in lesson
    assert "Turn isolation can hide session causes" in lesson
    assert "--include-session-context" in lesson
    assert "same session only" in lesson
    assert "stable envelope schema" in lesson
    assert "Every record has a stable schema" not in lesson
