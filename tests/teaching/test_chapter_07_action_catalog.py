"""Keep the teaching action inventory derived from the runtime."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER_07 = ROOT / "docs" / "teaching" / "07-tools"
CHAPTER_14 = ROOT / "docs" / "teaching" / "14-bring-your-own-agent"


def test_action_catalog_discovers_every_runtime_action() -> None:
    result = subprocess.run(
        [sys.executable, str(CHAPTER_07 / "action_catalog.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "count": 7,
        "actions": [
            {
                "action_class": "AddToDNCAction",
                "action_type": "add_to_dnc",
                "core_supported": True,
            },
            {
                "action_class": "CustomAction",
                "action_type": "custom",
                "core_supported": False,
            },
            {
                "action_class": "EndCallAction",
                "action_type": "end_call",
                "core_supported": True,
            },
            {
                "action_class": "RemoveFromDNCAction",
                "action_type": "remove_from_dnc",
                "core_supported": True,
            },
            {
                "action_class": "SendDTMFAction",
                "action_type": "send_dtmf",
                "core_supported": False,
            },
            {
                "action_class": "SendSMSAction",
                "action_type": "send_sms",
                "core_supported": False,
            },
            {
                "action_class": "TransferCallAction",
                "action_type": "transfer_call",
                "core_supported": False,
            },
        ],
    }


def test_action_lessons_name_the_current_inventory() -> None:
    exercises = (CHAPTER_07 / "EXERCISES.md").read_text(encoding="utf-8")
    chapter_07 = (CHAPTER_07 / "README.md").read_text(encoding="utf-8")
    chapter_14 = (CHAPTER_14 / "README.md").read_text(encoding="utf-8")
    combined = exercises + chapter_07

    assert "action_catalog.py" in exercises
    assert "Five types ship" not in chapter_07
    assert "the five\n  `SessionAction` types" not in chapter_14
    for action_class in (
        "EndCallAction",
        "TransferCallAction",
        "SendDTMFAction",
        "SendSMSAction",
        "AddToDNCAction",
        "RemoveFromDNCAction",
        "CustomAction",
    ):
        assert action_class in combined
