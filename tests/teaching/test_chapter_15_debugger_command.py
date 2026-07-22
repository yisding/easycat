"""Keep Chapter 15 on the maintained debugger CLI surface."""

from __future__ import annotations

import importlib.util
import shlex
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from typer.testing import CliRunner

from easycat.cli._app import _register_commands, app
from easycat.debug.export import export_debug_bundle
from easycat.runtime import JournalRecord

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "15-operate-in-production"


def _load_main_module() -> ModuleType:
    path = CHAPTER / "main.py"
    spec = importlib.util.spec_from_file_location("teaching_ch15_debugger", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_printed_debugger_command_invokes_the_registered_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chapter = _load_main_module()
    bundle = tmp_path / "path with spaces" / "ch15.bundle"
    bundle.parent.mkdir()
    record = JournalRecord(sequence=1, session_id="ch15", name="session_started")
    journal = SimpleNamespace(read=lambda: [record])
    export_debug_bundle(SimpleNamespace(journal=journal), bundle)
    calls: list[dict[str, object]] = []

    def fake_serve(bundle_obj, **kwargs):
        calls.append({"records": list(bundle_obj.records()), **kwargs})

    monkeypatch.setattr("easycat.debugger.serve_run_bundle", fake_serve)
    _register_commands()
    command = chapter.debugger_command(bundle)
    parts = shlex.split(command)

    assert parts[:3] == ["uv", "run", "easycat"]
    result = CliRunner().invoke(app, parts[3:])

    assert result.exit_code == 0, result.output
    assert calls[0]["port"] == 8765
    assert calls[0]["open_browser"] is True
    assert calls[0]["label"] == "ch15.bundle"
    assert calls[0]["records"][0]["name"] == "session_started"
