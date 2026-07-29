"""Keep Chapter 4's output evidence aligned with Chapter 3."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.teaching import _script_runner as script_runner

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "04-vad-preroll"


def test_vad_delivery_probe_uses_real_parrot_path() -> None:
    completed = script_runner.run(
        [sys.executable, str(CHAPTER / "delivery_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "delivery": {
            "accepted_chunks": 2,
            "committed_text": "hello from vad",
            "rejected_chunks": 1,
            "stage": "parrot",
        },
        "provider_events": [
            "stt.start",
            "stt.send",
            "stt.end",
            "stt.close",
            "tts.speak:hello from vad",
        ],
        "record_names": ["turn.started", "turn.ended", "parrot.delivery"],
    }


def test_vad_parrot_preserves_speak_acceptance_counts() -> None:
    source = (CHAPTER / "main.py").read_text(encoding="utf-8")

    assert "accepted_chunks, rejected_chunks = await speak" in source
    assert 'name="parrot.delivery"' in source
    assert '"accepted_chunks": accepted_chunks' in source
    assert '"rejected_chunks": rejected_chunks' in source
    assert "Preserve transport acceptance without claiming speaker playback" in source
