"""Keep Chapter 10's enabled stages and wrong-order evidence honest."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "10-cleaning-signal"


def _load_wrong_order(monkeypatch: pytest.MonkeyPatch):
    path = CHAPTER / "wrong_order.py"
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=object))
    spec = importlib.util.spec_from_file_location("teaching_ch10_wrong_order", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def test_wrong_order_pairs_vad_and_late_nr_by_frame_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chapter = _load_wrong_order(monkeypatch)
    rows: list[dict] = []

    class Journal:
        def append(self, **row) -> None:
            rows.append(row)

    class Transport:
        async def receive_audio(self):
            for chunk in ("frame-1", "frame-2"):
                yield chunk

    class Passthrough:
        async def process(self, chunk):
            return chunk

    class SilentVAD:
        async def process(self, _chunk):
            if False:
                yield None

    journal = Journal()
    pipeline = chapter.wrong_order_pipeline(
        Transport(), Passthrough(), Passthrough(), "nr-after-vad", journal, "ch10"
    )
    detector = chapter.MiniTurnDetector(SilentVAD(), journal, "ch10", record_before_nr=True)

    assert [event async for event in detector.frames(pipeline)] == []
    assert [(row["name"], row["data"]["frame_index"]) for row in rows] == [
        ("vad.processed_before_nr", 1),
        ("nr.applied_after_vad", 1),
        ("vad.processed_before_nr", 2),
        ("nr.applied_after_vad", 2),
    ]


async def test_aec_no_reference_mode_does_not_label_cleaned_vad_input_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chapter = _load_wrong_order(monkeypatch)
    rows: list[dict] = []

    class Journal:
        def append(self, **row) -> None:
            rows.append(row)

    class Transport:
        async def receive_audio(self):
            yield "frame"

    class Passthrough:
        async def process(self, chunk):
            return chunk

    class SilentVAD:
        async def process(self, _chunk):
            if False:
                yield None

    journal = Journal()
    pipeline = chapter.wrong_order_pipeline(
        Transport(), Passthrough(), Passthrough(), "aec-no-reference", journal, "ch10"
    )
    detector = chapter.MiniTurnDetector(SilentVAD(), journal, "ch10")

    assert [event async for event in detector.frames(pipeline)] == []
    assert rows == []
