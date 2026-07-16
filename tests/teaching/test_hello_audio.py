from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "00-hello-audio"


def _load_chapter(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(OutputStream=None))
    spec = importlib.util.spec_from_file_location("teaching_00_hello_audio", CHAPTER / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chunk_demo_models_source_wait_without_claiming_acoustic_latency(
    monkeypatch, capsys
) -> None:
    np = pytest.importorskip("numpy")
    chapter = _load_chapter(monkeypatch)
    events: list[object] = []
    now = 10.0

    class FakeStream:
        def __enter__(self):
            events.append("start")
            return self

        def __exit__(self, _exc_type, _exc, _traceback) -> None:
            events.extend(("stop", "close"))

        def write(self, block) -> None:
            events.append(("write", len(block)))

    def output_stream(**kwargs):
        events.append(("open", kwargs))
        return FakeStream()

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        events.append(("sleep", seconds))
        now += seconds

    monkeypatch.setattr(chapter.sd, "OutputStream", output_stream)
    monkeypatch.setattr(chapter.time, "monotonic", monotonic)
    monkeypatch.setattr(chapter.time, "sleep", sleep)

    chapter.play_chunked(np.zeros(3_200, dtype=np.int16), chunk_ms=200)

    assert events == [
        ("sleep", 0.2),
        (
            "open",
            {
                "blocksize": 3_200,
                "channels": 1,
                "dtype": "int16",
                "latency": "low",
                "samplerate": 16_000,
            },
        ),
        "start",
        ("write", 3_200),
        "stop",
        "close",
    ]
    output = capsys.readouterr().out
    assert "collecting first chunk" in output
    assert "time-to-first-write-return= 200.0ms" in output
    assert "time-to-first-sound" not in output
