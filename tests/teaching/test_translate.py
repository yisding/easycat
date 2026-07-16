from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

from easycat.debug.export import export_debug_bundle
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "15-operate-in-production"
SCRIPT = CHAPTER / "translate.py"
SOURCE_PATTERN = "'docs/teaching/13-swap-providers-and-transports/runs/ch13-openai-local-*.bundle'"
OUTPUT_PATH = "docs/teaching/15-operate-in-production/runs/translated.ndjson"


def _write_stage_bundle(path: Path, stage: str) -> None:
    journal = InMemoryRingBuffer()
    for name, state_key in (("stage_start", "state_before"), ("stage_complete", "state_after")):
        journal.append(
            kind=JournalRecordKind.EVENT,
            name=name,
            session_id="teaching-translate",
            turn_id="turn-1",
            data={"stage": stage, state_key: name},
        )
    export_debug_bundle(types.SimpleNamespace(journal=journal), path)


def test_translator_selects_newest_glob_match_and_creates_output_parent(
    tmp_path: Path,
) -> None:
    older = tmp_path / "ch13-openai-local-old.bundle"
    newer = tmp_path / "ch13-openai-local-new.bundle"
    _write_stage_bundle(older, "older")
    _write_stage_bundle(newer, "newer")
    os.utime(older, ns=(1, 1))
    os.utime(newer, ns=(2, 2))
    output = tmp_path / "missing" / "nested" / "translated.ndjson"

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "ch13-*.bundle"), str(output)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [record["name"] for record in records] == ["stage.newer.execute"]
    assert str(newer) in result.stdout
    assert str(output) in result.stdout


def test_translator_command_quotes_the_glob_and_uses_a_chapter_local_output() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    script_source = SCRIPT.read_text(encoding="utf-8")

    assert SOURCE_PATTERN in readme
    assert SOURCE_PATTERN in script_source
    assert OUTPUT_PATH in readme
    assert OUTPUT_PATH in script_source
    assert "runs/translated.ndjson" not in readme.replace(OUTPUT_PATH, "")
