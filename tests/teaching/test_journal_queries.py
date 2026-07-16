from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

from easycat.debug.testing import load_bundle

REPO_ROOT = Path(__file__).resolve().parents[2]
CHAPTER = REPO_ROOT / "docs" / "teaching" / "11-journal"


def _load_chapter_module(filename: str) -> types.ModuleType:
    path = CHAPTER / filename
    module_name = f"teaching_ch11_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_checked_in_bundle_supports_real_turn_queries() -> None:
    expected_turns = {
        "bug_01_empty_final.bundle": {"ch11-bug01-turn-1"},
        "bug_02_tts_stutter.bundle": {"ch11-bug02-turn-1"},
        "bug_03_ghost_interruption.bundle": {
            "ch11-bug03-turn-1",
            "ch11-bug03-turn-2",
        },
    }
    for filename, expected in expected_turns.items():
        fixture = load_bundle(CHAPTER / "bundles" / filename)
        actual = {
            record["turn_id"] for record in fixture.records() if record["turn_id"] is not None
        }
        assert actual == expected

    bundle = load_bundle(CHAPTER / "bundles" / "bug_03_ghost_interruption.bundle")

    first = bundle.filter_by_turn("ch11-bug03-turn-1")
    second = bundle.filter_by_turn("ch11-bug03-turn-2")

    assert [record["sequence"] for record in first] == list(range(2, 8))
    assert [record["sequence"] for record in second] == list(range(8, 14))
    assert {record["turn_id"] for record in first} == {"ch11-bug03-turn-1"}
    assert {record["turn_id"] for record in second} == {"ch11-bug03-turn-2"}


def test_investigate_queries_combine_public_bundle_filters() -> None:
    chapter = _load_chapter_module("investigate.py")
    bundle = load_bundle(CHAPTER / "bundles" / "bug_03_ghost_interruption.bundle")

    records = chapter.query_records(
        bundle,
        turn="ch11-bug03-turn-2",
        stage="stt",
        name="stt.final",
    )
    exact = chapter.query_records(bundle, sequence=9)

    assert [record["sequence"] for record in records] == [9]
    assert exact == records


def test_fixture_generator_preserves_turn_query_contract(tmp_path: Path) -> None:
    generator = _load_chapter_module("generate_bundles.py")

    generator.main(["--output-root", str(tmp_path)])

    generated = load_bundle(tmp_path / "bundles" / "bug_03_ghost_interruption.bundle")
    assert [
        record["sequence"] for record in generated.filter_by_turn("ch11-bug03-turn-2")
    ] == list(range(8, 14))


def test_query_lesson_uses_real_public_surfaces() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")

    assert "bundle.view" not in exercises
    assert "read-only and lazy" not in exercises
    assert "bytes_sent" not in exercises
    assert "RunBundle" in readme
    assert "JournalView" in readme
    assert "record representation differs" in readme
