"""Show both manual voice-stack ownership scopes without live providers.

Run with::

    uv run python docs/teaching/06-streaming-agent/voice_stack_cleanup_probe.py
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack

from easycat.runtime.capabilities import close_if_supported


class Resource:
    def __init__(self, name: str, events: list[str], *, fail_close: bool = False) -> None:
        self.name = name
        self.events = events
        self.fail_close = fail_close

    async def close(self) -> None:
        self.events.append(f"{self.name}.close")
        if self.fail_close:
            raise RuntimeError(f"{self.name} close failed")


class Transport:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def connect(self) -> None:
        self.events.append("transport.connect")

    async def disconnect(self) -> None:
        self.events.append("transport.disconnect")


class STT(Resource):
    async def start_stream(self) -> None:
        self.events.append("stt.start")

    async def end_stream(self) -> None:
        self.events.append("stt.end")


async def exercise(*, cancel_mid_turn: bool, fail_tts_close: bool = False) -> dict:
    events: list[str] = []
    outcome = "completed"
    error = None

    try:
        transport = Transport(events)
        async with AsyncExitStack() as resources:
            resources.push_async_callback(transport.disconnect)
            await transport.connect()

            vad = Resource("vad", events)
            resources.push_async_callback(close_if_supported, vad)
            client = Resource("client", events)
            resources.push_async_callback(close_if_supported, client)
            tts = Resource("tts", events, fail_close=fail_tts_close)
            resources.push_async_callback(close_if_supported, tts)

            stt: STT | None = STT("stt", events)
            try:
                await stt.start_stream()
                if cancel_mid_turn:
                    raise asyncio.CancelledError

                active_stt = stt
                stt = None
                try:
                    await active_stt.end_stream()
                finally:
                    await close_if_supported(active_stt)
            finally:
                if stt is not None:
                    try:
                        await stt.end_stream()
                    finally:
                        await close_if_supported(stt)
    except asyncio.CancelledError:
        outcome = "cancelled"
    except RuntimeError as exc:
        outcome = "cleanup_error"
        error = str(exc)

    return {"error": error, "events": events, "outcome": outcome}


async def main() -> None:
    payload = {
        "cancelled_turn": await exercise(cancel_mid_turn=True),
        "cleanup_failure": await exercise(cancel_mid_turn=False, fail_tts_close=True),
        "normal_turn": await exercise(cancel_mid_turn=False),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
