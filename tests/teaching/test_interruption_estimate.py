from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "09-interruption"


def _load_chapter(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(AsyncOpenAI=object))
    module_name = "teaching_09_interruption_estimate"
    spec = importlib.util.spec_from_file_location(module_name, CHAPTER / "estimate.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


async def test_rejected_audio_does_not_enter_heard_text_estimate(monkeypatch) -> None:
    chapter = _load_chapter(monkeypatch)
    rows: list[dict] = []

    class FakeTTS:
        async def synthesize(self, _payload):
            for data in (b"rejected", b"accepted"):
                yield types.SimpleNamespace(
                    type=chapter.TTSEventType.AUDIO,
                    audio=types.SimpleNamespace(data=data),
                )

    class FakeTransport:
        def __init__(self) -> None:
            self.results = iter((False, True))

        async def send_audio(self, _chunk) -> bool:
            return next(self.results)

    class FakeJournal:
        def append(self, **row) -> None:
            rows.append(row)

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put("hello world")
    await queue.put(None)
    ledger = chapter.TurnLedger()

    await chapter.drain_to_speaker(
        FakeTTS(),
        FakeTransport(),
        queue,
        types.SimpleNamespace(is_cancelled=False),
        ledger,
        FakeJournal(),
    )

    assert ledger.bytes_accepted == len(b"accepted")
    tts_record = next(row["data"] for row in rows if row["name"] == "stage.tts.execute")
    assert tts_record["bytes_accepted_so_far"] == len(b"accepted")


def test_heard_text_uses_accepted_byte_ratio(monkeypatch) -> None:
    chapter = _load_chapter(monkeypatch)
    ledger = chapter.TurnLedger(
        sentences_sent=["abcdefghijklmno"],
        bytes_accepted=chapter.TTS_BYTES_PER_SECOND // 2,
    )

    assert ledger.heard_text() == "abcdefg"
