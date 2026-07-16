"""Prove per-turn STT cleanup on normal and cancelled Chapter 4 paths.

uv run python docs/teaching/04-vad-preroll/stt_cleanup_probe.py
"""

from __future__ import annotations

import asyncio
import json

from main import parrot

from easycat.runtime import InMemoryRingBuffer


class ScriptedSTT:
    def __init__(self) -> None:
        self.started = 0
        self.ended = 0
        self.closed = 0

    async def start_stream(self) -> None:
        self.started += 1

    async def end_stream(self) -> None:
        self.ended += 1

    async def events(self):
        if False:
            yield None

    async def close(self) -> None:
        self.closed += 1


class ScriptedTransport:
    async def receive_audio(self):
        if False:
            yield None


class ScriptedDetector:
    def __init__(self, *, cancel_after_start: bool) -> None:
        self.cancel_after_start = cancel_after_start

    async def frames(self, _audio):
        yield "speech_started", None
        if self.cancel_after_start:
            raise asyncio.CancelledError
        yield "speech_ended", None


async def run_case(*, cancel_after_start: bool) -> dict[str, int]:
    stt = ScriptedSTT()
    try:
        await parrot(
            ScriptedTransport(),
            lambda: stt,
            ScriptedDetector(cancel_after_start=cancel_after_start),
            InMemoryRingBuffer(capacity=10),
            "cleanup-probe",
        )
    except asyncio.CancelledError:
        pass
    return {"started": stt.started, "ended": stt.ended, "closed": stt.closed}


async def probe() -> dict[str, dict[str, int]]:
    return {
        "normal_turn": await run_case(cancel_after_start=False),
        "cancelled_turn": await run_case(cancel_after_start=True),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2, sort_keys=True))
