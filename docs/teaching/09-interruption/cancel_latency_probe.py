"""Measure Chapter 9's software cancellation path without providers.

Run with::

    uv run python docs/teaching/09-interruption/cancel_latency_probe.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

_MISSING = object()


def load_cancel_chapter():
    """Load the sibling lesson while keeping the probe provider-free."""
    path = Path(__file__).with_name("cancel.py")
    spec = importlib.util.spec_from_file_location("teaching_ch09_cancel_latency", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    previous_openai = sys.modules.get("openai", _MISSING)
    sys.modules["openai"] = SimpleNamespace(AsyncOpenAI=object)
    try:
        spec.loader.exec_module(module)
    finally:
        if previous_openai is _MISSING:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = previous_openai
    return module


class ProbeJournal:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, **row) -> None:
        self.rows.append(row)


class ProbeCancel:
    def __init__(self, events: list[str]) -> None:
        self._cancelled = asyncio.Event()
        self._events = events

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        self._events.append("cancel.signalled")
        self._cancelled.set()

    async def wait(self) -> None:
        await self._cancelled.wait()


class ProbeTransport:
    def __init__(self, clock: dict[str, float], cleared: asyncio.Event, events: list[str]) -> None:
        self._clock = clock
        self._cleared = cleared
        self._events = events

    async def clear_audio(self) -> None:
        self._clock["now"] = 1.03
        self._events.append("transport.clear_audio.returned")
        self._cleared.set()


async def probe() -> dict[str, object]:
    chapter = load_cancel_chapter()
    clock = {"now": 1.0}
    events = ["bot.started"]
    cleared = asyncio.Event()
    cancel = ProbeCancel(events)
    transport = ProbeTransport(clock, cleared, events)
    journal = ProbeJournal()

    async def bot() -> None:
        await cancel.wait()
        await cleared.wait()
        clock["now"] = 1.08
        events.append("bot.returned")

    bot_task = asyncio.create_task(bot())
    real_monotonic = chapter.time.monotonic
    chapter.time.monotonic = lambda: clock["now"]
    try:
        bot_task, cancel_result, consumed = await chapter.route_barge_in(
            "speech_started", bot_task, cancel, transport, journal
        )
    finally:
        chapter.time.monotonic = real_monotonic

    complete = next(
        row["data"] for row in journal.rows if row["name"] == "interruption.cancel_complete"
    )
    return {
        "acoustic_silence_proven": False,
        "bot_task_cleared": bot_task is None,
        "cancel_to_bot_task_return_ms": round(complete["cancel_to_bot_task_return_ms"], 3),
        "cancel_token_cleared": cancel_result is None,
        "cancel_to_clear_audio_return_ms": round(complete["cancel_to_clear_audio_return_ms"], 3),
        "event_consumed": consumed,
        "events": events,
        "journal": [row["name"] for row in journal.rows],
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2, sort_keys=True))
