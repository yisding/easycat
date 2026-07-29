"""Keep Chapter 9's software cancellation milestones executable."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.teaching import _script_runner as script_runner

ROOT = Path(__file__).resolve().parents[2]
TEACHING = ROOT / "docs" / "teaching"
CHAPTER = TEACHING / "09-interruption"
CANCEL_SCRIPTS = [
    CHAPTER / "cancel.py",
    CHAPTER / "estimate.py",
    TEACHING / "10-cleaning-signal" / "main.py",
    TEACHING / "10-cleaning-signal" / "wrong_order.py",
]


def test_cancel_latency_probe_measures_software_milestones() -> None:
    completed = script_runner.run(
        [sys.executable, str(CHAPTER / "cancel_latency_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "acoustic_silence_proven": False,
        "bot_task_cleared": True,
        "cancel_to_bot_task_return_ms": 80.0,
        "cancel_token_cleared": True,
        "cancel_to_clear_audio_return_ms": 30.0,
        "event_consumed": False,
        "events": [
            "bot.started",
            "cancel.signalled",
            "transport.clear_audio.returned",
            "bot.returned",
        ],
        "journal": ["interruption.start", "interruption.cancel_complete"],
    }


def test_chapter_9_and_10_cancellation_copies_keep_latency_fields() -> None:
    stale = []
    for path in CANCEL_SCRIPTS:
        source = path.read_text(encoding="utf-8")
        if (
            'name="interruption.cancel_complete"' not in source
            or '"cancel_to_clear_audio_return_ms"' not in source
            or '"cancel_to_bot_task_return_ms"' not in source
        ):
            stale.append(path.relative_to(ROOT).as_posix())

    assert not stale, "Cancellation latency evidence drifted in: " + ", ".join(stale)
