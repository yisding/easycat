"""Exercise Chapter 9's real barge-in router without audio or providers.

Run with::

    uv run python docs/teaching/09-interruption/barge_in_turn_probe.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path


def load_cancel_chapter():
    path = Path(__file__).with_name("cancel.py")
    spec = importlib.util.spec_from_file_location("teaching_ch09_cancel_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Journal:
    def __init__(self) -> None:
        self.names: list[str] = []

    def append(self, *, name: str, **_kwargs) -> None:
        self.names.append(name)


class Transport:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def clear_audio(self) -> None:
        self.events.append("transport.clear_audio")


class Cancel:
    def __init__(self) -> None:
        self._cancelled = asyncio.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        self._cancelled.set()

    async def wait(self) -> None:
        await self._cancelled.wait()


async def main() -> None:
    chapter = load_cancel_chapter()
    events: list[str] = []
    journal = Journal()
    transport = Transport(events)
    cancel = Cancel()
    started = asyncio.Event()

    async def bot() -> None:
        events.append("bot.started")
        started.set()
        await cancel.wait()
        events.append("bot.stopped")

    bot_task = asyncio.create_task(bot())
    await started.wait()
    bot_task, cancel, consumed = await chapter.route_barge_in(
        "speech_started", bot_task, cancel, transport, journal
    )

    # ``consumed=False`` means the real coordinator falls through to its
    # ordinary STT branch with this same speech_started event.
    if not consumed:
        events.extend(("stt.start", "stt.frame", "stt.end", "stt.close"))

    payload = {
        "bot_task_cleared": bot_task is None,
        "cancel_token_cleared": cancel is None,
        "event_consumed": consumed,
        "events": events,
        "journal": journal.names,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
