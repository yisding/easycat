"""Exercise Chapter 2's streaming lifetime without a mic or provider key.

Run with::

    uv run python docs/teaching/02-transcribe/stream_lifecycle_probe.py
"""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stdout

from streaming import run_streaming

from easycat.events import STTEvent, STTEventType
from easycat.runtime import InMemoryRingBuffer


class ScriptedTransport:
    def __init__(self, events: list[str], *, fail_connect: bool = False) -> None:
        self.events = events
        self.fail_connect = fail_connect

    async def connect(self) -> None:
        self.events.append("transport.connect")
        if self.fail_connect:
            raise RuntimeError("transport connect failed")

    async def receive_audio(self):
        self.events.append("transport.receive")
        yield object()

    async def disconnect(self) -> None:
        self.events.append("transport.disconnect")


class ScriptedSTT:
    def __init__(
        self,
        events: list[str],
        *,
        fail_start: bool = False,
        fail_send: bool = False,
    ) -> None:
        self.events_log = events
        self.fail_start = fail_start
        self.fail_send = fail_send
        self.events_started = asyncio.Event()
        self.ended = asyncio.Event()

    async def start_stream(self) -> None:
        self.events_log.append("stt.start")
        if self.fail_start:
            raise RuntimeError("stt start failed")

    async def send_audio(self, _chunk) -> None:
        self.events_log.append("stt.send")
        # Let the consumer reach its wait so the failure case can prove that
        # TaskGroup cancels and joins it before cleanup begins.
        await self.events_started.wait()
        if self.fail_send:
            raise RuntimeError("stt send failed")

    async def end_stream(self) -> None:
        if not self.ended.is_set():
            self.events_log.append("stt.end")
            self.ended.set()

    async def events(self):
        self.events_log.append("stt.events.start")
        self.events_started.set()
        try:
            await self.ended.wait()
        except asyncio.CancelledError:
            self.events_log.append("stt.events.cancelled")
            raise
        self.events_log.append("stt.event.final")
        yield STTEvent(type=STTEventType.FINAL, text="probe transcript")

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
    fail_send: bool = False,
) -> dict[str, object]:
    events: list[str] = []
    error = None
    try:
        with redirect_stdout(io.StringIO()):
            await run_streaming(
                ScriptedSTT(events, fail_start=fail_start, fail_send=fail_send),
                ScriptedTransport(events, fail_connect=fail_connect),
                InMemoryRingBuffer(capacity=20),
                "ch02-lifecycle-probe",
                duration_s=0,
            )
    except Exception as exc:  # noqa: BLE001 - the probe reports each failure shape
        error = _root_message(exc)
    return {"error": error, "events": events}


async def probe() -> dict[str, dict[str, object]]:
    return {
        "connect_failure": await run_case(fail_connect=True),
        "feed_failure": await run_case(fail_send=True),
        "normal": await run_case(),
        "start_failure": await run_case(fail_start=True),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2, sort_keys=True))
