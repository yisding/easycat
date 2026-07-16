"""Show graceful and cancelled session scopes without providers.

Run with::

    uv run python \
        docs/teaching/13-swap-providers-and-transports/session_scope_probe.py
"""

from __future__ import annotations

import asyncio
import json


class Client:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self):
        self.events.append("client.open")
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        self.events.append("client.close")


class Session:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.closed = False

    async def __aenter__(self):
        self.events.append("session.start")
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        await self.stop(force=True)

    async def stop(self, *, force: bool = False) -> None:
        call = f"session.stop(force={force})"
        if self.closed:
            self.events.append(f"{call} -> no-op")
            return
        self.events.append(call)
        self.closed = True

    def export_postmortem(self) -> None:
        assert self.closed, "export must happen after session scope exits"
        self.events.append("session.export_postmortem")


async def run_scenario(*, cancelled: bool) -> dict[str, object]:
    events: list[str] = []
    session = Session(events)

    async with Client(events):
        try:
            async with session:
                events.append("session.work")
                if cancelled:
                    events.append("outer.cancel")
                    raise asyncio.CancelledError

                # Mirrors wait_for_shutdown_signal(): after SIGINT/SIGTERM,
                # stop without force so in-flight work can drain.
                events.append("shutdown.signal")
                await session.stop()
        except asyncio.CancelledError:
            # The owner handles cancellation before doing postmortem work.
            pass
        session.export_postmortem()

    return {"events": events, "session_closed": session.closed}


async def main() -> None:
    graceful = await run_scenario(cancelled=False)
    cancelled = await run_scenario(cancelled=True)
    print(json.dumps({"graceful": graceful, "cancelled": cancelled}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
