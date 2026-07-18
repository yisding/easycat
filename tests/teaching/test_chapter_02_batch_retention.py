"""Keep Chapter 2's temporary microphone audio scoped and privacy-explicit."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from easycat.debug.testing import load_bundle

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "02-transcribe"


def _load_batch(monkeypatch: pytest.MonkeyPatch):
    sounddevice = types.ModuleType("sounddevice")
    monkeypatch.setitem(sys.modules, "sounddevice", sounddevice)

    path = CHAPTER / "batch.py"
    spec = importlib.util.spec_from_file_location("teaching_ch02_batch", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_batch_deletes_raw_wav_before_export(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    chapter = _load_batch(monkeypatch)
    chapter.RUNS_DIR = tmp_path / "runs"
    chapter.SESSION_ID = "ch02-batch-retention-test"
    monkeypatch.setenv("OPENAI_API_KEY", "provider-free-probe")
    observed_paths: list[Path] = []

    def fake_record(path: Path) -> None:
        path.write_bytes(b"raw microphone bytes")

    def fake_transcribe(path: Path):
        assert path.exists()
        observed_paths.append(path)

        async def result() -> str:
            return "sensitive transcript"

        return result()

    monkeypatch.setattr(chapter, "record_wav", fake_record)
    monkeypatch.setattr(chapter, "transcribe_file", fake_transcribe)

    await chapter.main()

    wav_path = observed_paths[0]
    assert not wav_path.exists()
    assert not wav_path.parent.exists()

    bundle_path = chapter.RUNS_DIR / f"{chapter.SESSION_ID}.bundle"
    records = list(load_bundle(bundle_path).records())
    by_name = {record["name"]: record for record in records}
    assert by_name["recording.complete"]["data"] == {
        "duration_s": chapter.DURATION_S,
        "filename": "ch02-batch.wav",
        "retention": "temporary",
    }
    assert by_name["recording.cleaned"]["data"] == {
        "deleted": True,
        "filename": "ch02-batch.wav",
    }
    assert by_name["stt.final"]["data"]["text"] == "sensitive transcript"
    assert str(wav_path.parent) not in json.dumps(records)


@pytest.mark.asyncio
async def test_batch_deletes_raw_wav_when_transcription_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    chapter = _load_batch(monkeypatch)
    chapter.RUNS_DIR = tmp_path / "runs"
    monkeypatch.setenv("OPENAI_API_KEY", "provider-free-probe")
    observed_paths: list[Path] = []

    def fake_record(path: Path) -> None:
        path.write_bytes(b"raw microphone bytes")

    def failing_transcribe(path: Path):
        assert path.exists()
        observed_paths.append(path)

        async def result() -> str:
            raise RuntimeError("provider failed")

        return result()

    monkeypatch.setattr(chapter, "record_wav", fake_record)
    monkeypatch.setattr(chapter, "transcribe_file", failing_transcribe)

    with pytest.raises(RuntimeError, match="provider failed"):
        await chapter.main()

    wav_path = observed_paths[0]
    assert not wav_path.exists()
    assert not wav_path.parent.exists()
    assert not chapter.RUNS_DIR.exists()
