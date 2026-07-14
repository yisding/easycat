from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

from easycat.transports.local import LocalTransport, LocalTransportConfig

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
                    audio=chapter.AudioChunk(data=data, format=chapter.PCM16_MONO_24K),
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


async def test_partial_local_transport_chunk_credits_enqueued_head(monkeypatch) -> None:
    chapter = _load_chapter(monkeypatch)
    frame_bytes = chapter.TTS_BYTES_PER_SECOND * chapter.LOCAL_OUTPUT_FRAME_MS // 1000

    class FakeTTS:
        async def synthesize(self, _payload):
            yield types.SimpleNamespace(
                type=chapter.TTSEventType.AUDIO,
                audio=chapter.AudioChunk(
                    data=bytes(frame_bytes * 2),
                    format=chapter.PCM16_MONO_24K,
                ),
            )

    class FakeJournal:
        def append(self, **_row) -> None: ...

    transport = LocalTransport(
        LocalTransportConfig(
            audio_format=chapter.PCM16_MONO_24K,
            frame_duration_ms=chapter.LOCAL_OUTPUT_FRAME_MS,
            max_pending_out_chunks=1,
        )
    )
    transport._connected = True
    queue: asyncio.Queue = asyncio.Queue()
    await queue.put("hello world")
    await queue.put(None)
    ledger = chapter.TurnLedger()

    await chapter.drain_to_speaker(
        FakeTTS(),
        transport,
        queue,
        types.SimpleNamespace(is_cancelled=False),
        ledger,
        FakeJournal(),
    )

    assert transport._out_queue.qsize() == 1
    assert ledger.bytes_accepted == frame_bytes


def test_heard_text_uses_accepted_byte_ratio(monkeypatch) -> None:
    chapter = _load_chapter(monkeypatch)
    ledger = chapter.TurnLedger(
        sentences_sent=["abcdefghijklmno"],
        bytes_accepted=chapter.TTS_BYTES_PER_SECOND // 2,
    )

    assert ledger.heard_text() == "abcdefg"
