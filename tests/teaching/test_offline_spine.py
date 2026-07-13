"""Keep the credential-free checkpoint spine complete and executable."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEACHING = ROOT / "docs" / "teaching"
SPINE = TEACHING / "offline_spine.py"


def _load_spine():
    spec = importlib.util.spec_from_file_location("teaching_offline_spine", SPINE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_offline_spine_tracks_one_checkpoint_per_chapter() -> None:
    spine = _load_spine()
    rows = spine.catalog()
    chapter_dirs = sorted(path.name for path in TEACHING.iterdir() if path.name[:2].isdigit())

    assert [row["chapter"] for row in rows] == list(range(16))
    assert [row["folder"] for row in rows] == chapter_dirs
    assert len({row["command"] for row in rows}) == len(rows)
    for row in rows:
        path = ROOT / "docs" / "teaching" / row["folder"] / row["script"]
        assert path.is_file()
        assert row["command"] == f"uv run python {path.relative_to(ROOT).as_posix()}"


def test_offline_spine_json_list_is_documented() -> None:
    completed = subprocess.run(
        [sys.executable, str(SPINE), "--json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    readme = (TEACHING / "README.md").read_text(encoding="utf-8")
    root_readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert payload["mode"] == "list"
    assert payload["count"] == 16
    assert len(payload["checkpoints"]) == 16
    assert "Hardware-free checkpoint spine" in readme
    assert "uv run python docs/teaching/offline_spine.py --json" in readme
    assert "strips all `*_API_KEY` variables" in readme
    assert "[hardware-free teaching spine](docs/teaching/#hardware-free-checkpoint-spine)" in (
        root_readme
    )
    assert "uv run python docs/teaching/offline_spine.py --run --jobs 4" in root_readme


def test_offline_spine_runs_every_checkpoint_without_credentials() -> None:
    root_entries_before = {path.name for path in ROOT.iterdir()}
    before = {path.relative_to(TEACHING) for path in TEACHING.rglob("*")}
    completed = subprocess.run(
        [sys.executable, str(SPINE), "--run", "--jobs", "4", "--json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    payload = json.loads(completed.stdout)

    assert payload["mode"] == "run"
    assert payload["count"] == 16
    assert payload["passed"] == 16
    assert payload["failed"] == 0
    assert {row["status"] for row in payload["checkpoints"]} == {"pass"}
    assert {row["returncode"] for row in payload["checkpoints"]} == {0}
    after = {path.relative_to(TEACHING) for path in TEACHING.rglob("*")}
    assert after == before
    assert {path.name for path in ROOT.iterdir()} == root_entries_before


def test_offline_spine_documents_workspace_hygiene() -> None:
    source = SPINE.read_text(encoding="utf-8")
    readme = (TEACHING / "README.md").read_text(encoding="utf-8")

    assert 'environment["PYTHONDONTWRITEBYTECODE"] = "1"' in source
    assert "leave files in the checkout" in source
    assert "full run leaves the checkout unchanged" in readme
