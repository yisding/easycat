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
