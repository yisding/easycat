"""Keep Chapter 3's timeout analysis aligned with asyncio behavior."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from easycat.debug.export import export_debug_bundle
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "03-parrot-naive"
INSPECTOR = CHAPTER / "inspect_timeout.py"


def test_timeout_inspector_uses_latest_stt_event_and_reports_overshoot(tmp_path: Path) -> None:
    journal = InMemoryRingBuffer(capacity=10)
    for name, data in (
        ("stt.partial", {"stage": "stt", "text": "The capital", "offset_ms": 100.0}),
        ("stt.final", {"stage": "stt", "text": "The capital is", "offset_ms": 150.0}),
        (
            "parrot.fire",
            {
                "stage": "parrot",
                "committed_text": "The capital is",
                "silence_timeout_s": 0.5,
                "offset_ms": 655.0,
            },
        ),
        (
            "stt.received",
            {
                "stage": "stt",
                "event_id": 3,
                "event_type": "partial",
                "text": "Paris",
                "offset_ms": 700.0,
                "queue_depth_before_put": 0,
            },
        ),
        (
            "stt.partial",
            {
                "stage": "stt",
                "event_id": 3,
                "event_type": "partial",
                "text": "Paris",
                "offset_ms": 950.0,
                "received_offset_ms": 700.0,
                "consumer_lag_ms": 250.0,
                "queue_depth_after_get": 0,
            },
        ),
    ):
        journal.append(
            kind=JournalRecordKind.EVENT,
            name=name,
            session_id="chapter-03-timeout",
            data=data,
        )

    bundle = tmp_path / "chapter-03.bundle"
    export_debug_bundle(SimpleNamespace(journal=journal), bundle)
    result = subprocess.run(
        [sys.executable, str(INSPECTOR), str(bundle)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["fires"] == [
        {
            "configured_timeout_ms": 500.0,
            "consumer_backlog_ms": 250.0,
            "fire": {
                "name": "parrot.fire",
                "offset_ms": 655.0,
                "sequence": 3,
                "text": "The capital is",
            },
            "next_partial": {
                "name": "stt.partial",
                "offset_ms": 950.0,
                "sequence": 5,
                "text": "Paris",
            },
            "next_partial_ingress": {
                "name": "stt.received",
                "offset_ms": 700.0,
                "sequence": 4,
                "text": "Paris",
            },
            "observed_silence_ms": 505.0,
            "post_fire_consumer_gap_ms": 295.0,
            "post_fire_ingress_gap_ms": 45.0,
            "scheduler_overshoot_ms": 5.0,
            "trigger_record": {
                "name": "stt.final",
                "offset_ms": 150.0,
                "sequence": 2,
                "text": "The capital is",
            },
        }
    ]


def test_timeout_lesson_rejects_exact_deadline_claim() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")

    assert "inspect_timeout.py" in readme
    assert "inspect_timeout.py" in exercises
    assert "last-partial timestamp plus 500 ms" not in readme
    assert "It should be exactly" not in exercises
    assert "latest `stt.partial` or `stt.final`" in exercises
    assert "post_fire_ingress_gap_ms" in exercises
    assert "post_fire_consumer_gap_ms" in exercises
    assert "consumer_backlog_ms" in exercises
