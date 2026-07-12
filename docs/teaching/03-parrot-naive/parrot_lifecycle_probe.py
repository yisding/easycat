"""Exercise Chapter 3's inherited task/resource scope without providers.

Run with::

    uv run python docs/teaching/03-parrot-naive/parrot_lifecycle_probe.py
"""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stdout

from main import run_parrot

from easycat.events import STTEvent, STTEventType
from easycat.runtime import InMemoryRingBuffer


class ScriptedTransport:
    def __init__(
        self,
        events: list[str],
        provider_started: asyncio.Event,
        *,
        fail_connect: bool = False,
        fail_receive: bool = False,
    ) -> None:
        self.events = events
        self.provider_started = provider_started
        self.fail_connect = fail_connect
        self.fail_receive = fail_receive

    async def connect(self) -> None:
        self.events.append("transport.connect")
        if self.fail_connect:
            raise RuntimeError("transport connect failed")

    async def receive_audio(self):
        self.events.append("transport.receive")
        if self.fail_receive:
            await self.provider_started.wait()
            self.events.append("transport.receive.failed")
            raise RuntimeError("microphone receive failed")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.events.append("transport.receive.cancelled")
            raise
        if False:
            yield None

    async def disconnect(self) -> None:
        self.events.append("transport.disconnect")


class ScriptedSTT:
    def __init__(
        self,
        events: list[str],
        provider_started: asyncio.Event,
        *,
        fail_start: bool = False,
        hold_events: bool = False,
    ) -> None:
        self.events_log = events
        self.provider_started = provider_started
        self.fail_start = fail_start
        self.hold_events = hold_events
        self.ended = False

    async def start_stream(self) -> None:
        self.events_log.append("stt.start")
        if self.fail_start:
            raise RuntimeError("stt start failed")

    async def send_audio(self, _chunk) -> None:
        self.events_log.append("stt.send")

    async def events(self):
        self.events_log.append("stt.events.start")
        self.provider_started.set()
        if self.hold_events:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.events_log.append("stt.events.cancelled")
                raise
        else:
            yield STTEvent(type=STTEventType.FINAL, text="probe complete")
            self.events_log.append("stt.events.end")

    async def end_stream(self) -> None:
        if not self.ended:
            self.events_log.append("stt.end")
            self.ended = True

    async def close(self) -> None:
        self.events_log.append("stt.close")


def _root_message(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return _root_message(exc.exceptions[0])
    return str(exc)


async def run_case(
    *,
    fail_connect: bool = False,
    fail_start: bool = False,
    fail_receive: bool = False,
) -> dict[str, object]:
    events: list[str] = []
    provider_started = asyncio.Event()
    error = None
    try:
        with redirect_stdout(io.StringIO()):
            await run_parrot(
                ScriptedSTT(
                    events,
                    provider_started,
                    fail_start=fail_start,
                    hold_events=fail_receive,
                ),
                ScriptedTransport(
                    events,
                    provider_started,
                    fail_connect=fail_connect,
                    fail_receive=fail_receive,
                ),
                InMemoryRingBuffer(capacity=20),
            )
    except Exception as exc:  # noqa: BLE001 - report each failure shape as JSON
        error = _root_message(exc)
    return {"error": error, "events": events}


async def probe() -> dict[str, dict[str, object]]:
    return {
        "connect_failure": await run_case(fail_connect=True),
        "feed_failure": await run_case(fail_receive=True),
        "normal_event_end": await run_case(),
        "start_failure": await run_case(fail_start=True),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2, sort_keys=True))
