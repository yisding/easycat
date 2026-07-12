"""Show production session and caller-owned client scopes without providers.

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
        self.events.append("session.stop(force=True)")
        self.closed = True

    def export_postmortem(self) -> None:
        assert self.closed, "export must happen after session scope exits"
        self.events.append("session.export_postmortem")


async def main() -> None:
    events: list[str] = []
    session = Session(events)

    async with Client(events):
        async with session:
            events.append("session.work")
        session.export_postmortem()

    print(json.dumps({"events": events, "session_closed": session.closed}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
