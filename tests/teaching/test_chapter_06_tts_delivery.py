"""Keep Chapter 6's streamed TTS delivery evidence executable."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.teaching._source_guards import assert_sources_match

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "06-streaming-agent"
STREAMING_SCRIPTS = [
    CHAPTER / "main.py",
    ROOT / "docs" / "teaching" / "07-tools" / "main.py",
    ROOT / "docs" / "teaching" / "07-tools" / "blocking_tool.py",
    ROOT / "docs" / "teaching" / "08-smart-turn" / "main.py",
]


def test_streamed_tts_probe_preserves_sentence_and_turn_counts() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER / "tts_delivery_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "all_rejected": {
            "accepted_chunks": 0,
            "gap_available": False,
            "outcome": "all_chunks_rejected",
            "rejected_chunks": 2,
            "sentence_counts": [[0, 1], [0, 1]],
            "turn_counts_match": True,
        },
        "mixed_across_sentences": {
            "accepted_chunks": 1,
            "gap_available": True,
            "outcome": "first_audio_accepted",
            "rejected_chunks": 1,
            "sentence_counts": [[0, 1], [1, 0]],
            "turn_counts_match": True,
        },
        "no_audio": {
            "accepted_chunks": 0,
            "gap_available": False,
            "outcome": "no_chunks_produced",
            "rejected_chunks": 0,
            "sentence_counts": [[0, 0]],
            "turn_counts_match": True,
        },
    }


def test_streaming_chapter_copies_preserve_delivery_evidence() -> None:
    assert_sources_match(
        STREAMING_SCRIPTS,
        required=(
            '"accepted_chunks": sentence_accepted',
            '"rejected_chunks": sentence_rejected',
            '"tts_accepted_chunks": accepted_chunks',
            '"tts_rejected_chunks": rejected_chunks',
            "transport rejected all",
            "TTS produced no audio",
        ),
        label="Streaming TTS delivery evidence",
    )
