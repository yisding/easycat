"""Keep Chapter 2's first bundle-query exercise executable."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from easycat.debug.export import export_debug_bundle
from easycat.debug.testing import load_bundle
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind

ROOT = Path(__file__).resolve().parents[2]
EXERCISES = ROOT / "docs" / "teaching" / "02-transcribe" / "EXERCISES.md"


def test_runbundle_linear_and_stage_queries_select_the_same_partials(tmp_path: Path) -> None:
    journal = InMemoryRingBuffer(capacity=10)
    for name, stage, text in (
        ("stt.partial", "stt", "hel"),
        ("transport.audio", "transport", ""),
        ("stt.partial", "stt", "hello"),
        ("stt.final", "stt", "hello"),
    ):
        journal.append(
            kind=JournalRecordKind.EVENT,
            name=name,
            session_id="chapter-02-query",
            data={"stage": stage, "text": text},
        )

    path = tmp_path / "chapter-02.bundle"
    export_debug_bundle(SimpleNamespace(journal=journal), path)
    bundle = load_bundle(path)

    linear = [record for record in bundle.records() if record["name"] == "stt.partial"]
    structured = [
        record for record in bundle.filter_by_stage("stt") if record["name"] == "stt.partial"
    ]

    assert [record["sequence"] for record in linear] == [1, 3]
    assert structured == linear


def test_exercise_uses_runbundle_public_query_surface() -> None:
    exercises = EXERCISES.read_text(encoding="utf-8")

    assert "b.view" not in exercises
    assert 'b.filter_by_stage("stt")' in exercises
    assert "returns a `RunBundle`" in exercises
    assert "typed `JournalRecord`" in exercises
