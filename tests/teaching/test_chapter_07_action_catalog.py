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
