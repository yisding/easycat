"""Keep Chapter 14's workflow-state boundary explicit and executable."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "14-bring-your-own-agent"


def load_probe():
    module_name = "test_chapter_14_workflow_state_probe"
    spec = importlib.util.spec_from_file_location(module_name, CHAPTER / "workflow_state_probe.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def test_workflow_state_probe_uses_metadata_allowlist() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER / "workflow_state_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    expected_state = {
        "message_count": 3,
        "history_roles": ["system", "user", "assistant"],
        "last_assistant_chars": 31,
        "session_action_pending": True,
    }
    assert payload == {
        "reply": "Sure, ending the call. Goodbye.",
        "action": {
            "type": "end_call",
            "reason": "user requested hang-up",
        },
        "bridge_snapshot": {
            "display_name": "MyWorkflow",
            "mode": "deep",
            "workflow_state": expected_state,
        },
        "artifact_payload": expected_state,
    }


def test_workflow_state_probe_restores_optional_sdk_module(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setitem(sys.modules, "openai", sentinel)

    load_probe()

    assert sys.modules["openai"] is sentinel


def test_chapter_teaches_author_owned_snapshot_boundary() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    normalized = " ".join(f"{readme}\n{exercises}".split())

    assert "workflow_state_probe.py" in normalized
    assert "metadata-only allowlist" in normalized
    assert "author-owned artifact data" in normalized
    assert "never use a real credential" in normalized
    assert "much broader `workflow.__dict__` serialization" in normalized
