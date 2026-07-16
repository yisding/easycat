"""Keep Chapter 0 honest about audio timing and stream ownership."""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Sized
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "00-hello-audio"


def _load_main(monkeypatch: pytest.MonkeyPatch, stream: object):
    sounddevice = types.ModuleType("sounddevice")
    sounddevice.OutputStream = lambda **_kwargs: stream  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sounddevice", sounddevice)

    path = CHAPTER / "main.py"
    spec = importlib.util.spec_from_file_location("teaching_ch00_audio", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeStream:
    def __init__(self, *, fail_write: bool = False) -> None:
        self.fail_write = fail_write
        self.events: list[object] = []

    def __enter__(self):
        self.events.append("start")
        return self

    def __exit__(self, exc_type, _exc, _traceback) -> None:
        self.events.extend(("stop", "close", ("exit_error", exc_type)))

    def write(self, block: Sized) -> None:
        self.events.append(("write", len(block)))
        if self.fail_write:
            raise RuntimeError("device write failed")


def test_chunked_playback_models_source_wait_and_names_enqueue_boundary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    np = pytest.importorskip("numpy")
    stream = _FakeStream()
    chapter = _load_main(monkeypatch, stream)
    sleeps: list[float] = []
    monkeypatch.setattr(chapter.time, "sleep", sleeps.append)

    chapter.play_chunked(np.zeros(320, dtype=np.int16), chunk_ms=10)

    assert sleeps == [0.01]
    assert stream.events == [
        "start",
        ("write", 160),
        ("write", 160),
        "stop",
        "close",
        ("exit_error", None),
    ]
    output = capsys.readouterr().out
    assert "time-to-first-write-return=" in output
    assert "time-to-first-sound" not in output


def test_chunked_playback_closes_stream_when_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    np = pytest.importorskip("numpy")
    stream = _FakeStream(fail_write=True)
    chapter = _load_main(monkeypatch, stream)
    monkeypatch.setattr(chapter.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="device write failed"):
        chapter.play_chunked(np.zeros(160, dtype=np.int16), chunk_ms=10)

    assert stream.events == [
        "start",
        ("write", 160),
        "stop",
        "close",
        ("exit_error", RuntimeError),
    ]


def test_lesson_distinguishes_enqueue_from_playback_and_teaches_scope() -> None:
    readme = (CHAPTER / "README.md").read_text(encoding="utf-8")
    exercises = (CHAPTER / "EXERCISES.md").read_text(encoding="utf-8")
    lesson = " ".join(f"{readme}\n{exercises}".split())

    assert "time-to-first-write-return" in lesson
    assert "not proof that the speaker played" in lesson
    assert "context manager" in lesson
    assert "guarantees stop + close" in lesson
