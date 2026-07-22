"""Keep Chapter 6's first-audio attribution executable and additive."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from easycat.debug.export import export_debug_bundle
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "06-streaming-agent"
MEASURER = CHAPTER / "measure_start.py"


def test_start_measurer_attributes_model_startup_before_first_token(tmp_path: Path) -> None:
    journal = InMemoryRingBuffer(capacity=10)
    for name, data in (
        ("stt.final", {"stage": "stt", "text": "hello", "t_ms": 1_000.0}),
        ("agent.first_token", {"stage": "agent", "t_ms": 2_100.0}),
        ("tts.first_audio", {"stage": "tts", "t_ms": 2_400.0}),
        (
            "stage.tts.execute",
            {"stage": "tts", "text": "Hello there.", "elapsed_ms": 450.0},
        ),
        (
            "stage.tts.execute",
            {"stage": "tts", "text": "How are you?", "elapsed_ms": 350.0},
        ),
    ):
        journal.append(
            kind=JournalRecordKind.EVENT,
            name=name,
            session_id="chapter-06-start",
            data=data,
        )

    bundle = tmp_path / "chapter-06.bundle"
    export_debug_bundle(SimpleNamespace(journal=journal), bundle)
    result = subprocess.run(
        [sys.executable, str(MEASURER), str(bundle)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["turns"] == [
        {
            "first_token_to_first_audio_ms": 300.0,
            "sentence_tts_ms": [450.0, 350.0],
            "stt_final_sequence": 1,
            "stt_final_to_first_audio_ms": 1_400.0,
            "stt_final_to_first_token_ms": 1_100.0,
            "text": "hello",
        }
    ]
