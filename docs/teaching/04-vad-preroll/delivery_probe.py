"""Exercise Chapter 4's output-acceptance evidence without providers.

Run with::

    uv run python docs/teaching/04-vad-preroll/delivery_probe.py
"""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stdout

import main as chapter

from easycat.events import STTEvent, STTEventType
from easycat.runtime import InMemoryRingBuffer


class ScriptedTransport:
    async def receive_audio(self):
        if False:
            yield None


class ScriptedDetector:
    async def frames(self, _audio):
        yield "speech_started", None
        yield "frame", object()
        yield "speech_ended", None


class ScriptedSTT:
    def __init__(self, events: list[str]) -> None:
        self.events_log = events

    async def start_stream(self) -> None:
        self.events_log.append("stt.start")

    async def send_audio(self, _chunk) -> None:
        self.events_log.append("stt.send")

    async def end_stream(self) -> None:
        self.events_log.append("stt.end")

    async def events(self):
        yield STTEvent(type=STTEventType.FINAL, text="hello from vad")

    async def close(self) -> None:
        self.events_log.append("stt.close")


async def probe() -> dict[str, object]:
    provider_events: list[str] = []
    journal = InMemoryRingBuffer(capacity=10)
    stt = ScriptedSTT(provider_events)

    async def scripted_speak(_transport, text: str) -> tuple[int, int]:
        provider_events.append(f"tts.speak:{text}")
        return 2, 1

    chapter.speak = scripted_speak
    with redirect_stdout(io.StringIO()):
        await chapter.parrot(
            ScriptedTransport(),
            lambda: stt,
            ScriptedDetector(),
            journal,
            "ch04-delivery-probe",
        )

    records = journal.read()
    delivery = next(record.data for record in records if record.name == "parrot.delivery")
    return {
        "delivery": {key: value for key, value in delivery.items() if key != "t_ms"},
        "provider_events": provider_events,
        "record_names": [record.name for record in records],
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2, sort_keys=True))
