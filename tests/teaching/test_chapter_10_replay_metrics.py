"""Keep Chapter 10's replay reference and signal metrics executable."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from tests.teaching import _script_runner as script_runner

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "10-cleaning-signal"


def load_replay():
    module_name = "test_chapter_10_replay"
    spec = importlib.util.spec_from_file_location(module_name, CHAPTER / "replay.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def test_replay_metrics_probe_enforces_reference_and_records_signal_change() -> None:
    runs_dir = CHAPTER / "runs"
    runs_dir_existed = runs_dir.exists()
    before = set(runs_dir.iterdir()) if runs_dir.exists() else set()
    completed = script_runner.run(
        [sys.executable, str(CHAPTER / "replay_metrics_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "aligned": {
            "first_frame": {
                "cleaned_rms": 250.0,
                "frame_index": 1,
                "input_rms": 1000.0,
                "reference_fed": True,
                "stage": "audio",
                "vad_starts": 1,
            },
            "frame_records": 2,
            "summary": {
                "cleaned_rms": 250.0,
                "input_rms": 1000.0,
                "mic_frames": 2,
                "reference_frames_fed": 2,
                "rms_change_db": -12.041,
                "stage": "audio",
                "vad_starts": 1,
            },
        },
        "errors": {
            "missing_reference": "--ref is required when --aec on",
            "short_reference": "mic and ref frame counts differ for AEC: 2 vs 1",
        },
    }
    after = set(runs_dir.iterdir()) if runs_dir.exists() else set()
    assert runs_dir.exists() is runs_dir_existed
    assert after == before


def test_replay_source_records_promised_per_frame_and_summary_metrics() -> None:
    source = (CHAPTER / "replay.py").read_text(encoding="utf-8")

    assert 'name="replay.frame"' in source
    assert '"reference_fed": ref_fed' in source
    assert '"input_rms"' in source
    assert '"cleaned_rms"' in source
    assert '"rms_change_db"' in source
    assert 'raise SystemExit("--ref is required when --aec on")' in source


def test_replay_journal_retains_more_than_default_capacity() -> None:
    replay = load_replay()
    audio_format = replay.AudioFormat(sample_rate=8_000, channels=1, sample_width=2)
    frame_bytes = audio_format.sample_rate * replay.FRAME_MS // 1000 * audio_format.frame_size
    frame_count = 10_050
    capacity = (
        replay._frame_count(bytes(frame_bytes * frame_count), audio_format)
        + replay.REPLAY_METADATA_RECORDS
    )
    journal = replay.InMemoryRingBuffer(capacity=capacity)

    for name in ["audio.config", *(["replay.frame"] * frame_count), "replay.summary"]:
        journal.append(
            kind=replay.JournalRecordKind.EVENT,
            name=name,
            session_id="retention-regression",
        )

    records = journal.read()
    assert len(records) == frame_count + 2
    assert records[0].name == "audio.config"
    assert sum(record.name == "replay.frame" for record in records) == frame_count
    assert records[-1].name == "replay.summary"
