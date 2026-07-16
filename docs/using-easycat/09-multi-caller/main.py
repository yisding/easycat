"""Chapter 9 — Check EasyCat's multi-caller admission lifecycle offline.

Dependencies:
    uv sync --group dev

Run:
    uv run python docs/using-easycat/09-multi-caller/main.py
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from easycat.server import BearerTokenAuth, CapacityGate, enforce_bind_guard

TOKEN = "chapter-9-demo-token"


@dataclass(frozen=True)
class Request:
    authorization_header: str | None = None
    query_token: str | None = None


class DemoSession:
    """Small lifecycle probe created once per accepted connection."""

    def __init__(self, number: int, events: list[str]) -> None:
        self.number = number
        self._events = events
        self._stopped = False

    async def start(self) -> None:
        self._events.append(f"session {self.number} started")

    async def stop(self, *, force: bool = False) -> None:
        if self._stopped:
            return
        self._stopped = True
        mode = "forced" if force else "graceful"
        self._events.append(f"session {self.number} stopped {mode}")


class LocalSupervisor:
    """Transport-free model of the admission collaborators used by VoiceServer."""

    def __init__(self, *, max_sessions: int, events: list[str]) -> None:
        self.auth = BearerTokenAuth(token=TOKEN)
        self.gate: CapacityGate[str] = CapacityGate(max_sessions)
        self.events = events
        self.sessions: dict[str, DemoSession] = {}
        self.created = 0

    async def connect(self, key: str, request: Request) -> tuple[str, DemoSession | None]:
        result = self.auth.authorize(request)
        if not result.allowed:
            return f"auth:{result.reason}", None
        if not self.gate.try_acquire():
            reason = "draining" if self.gate.is_draining else "capacity"
            return reason, None

        self.created += 1
        session = DemoSession(self.created, self.events)
        self.sessions[key] = session
        self.gate.track(key)
        try:
            await session.start()
        except BaseException:
            # Startup may have allocated partial resources. Restore admission
            # bookkeeping synchronously before attempting best-effort teardown,
            # so even cancellation cannot strand the only capacity slot.
            self.sessions.pop(key, None)
            self.gate.untrack(key)
            self.gate.release()
            try:
                await session.stop(force=True)
            except BaseException:
                pass
            raise
        return "accepted", session

    async def disconnect(self, key: str) -> None:
        session = self.sessions.pop(key)
        try:
            await session.stop()
        finally:
            # Admission bookkeeping cannot depend on provider/session teardown
            # succeeding; cancellation and stop failures must free the slot.
            self.gate.untrack(key)
            self.gate.release()

    async def shutdown(self) -> None:
        self.gate.start_draining()
        pairs = tuple(self.sessions.items())
        await self.gate.drain(
            lambda: pairs,
            drain_timeout_s=1.0,
            force_timeout_s=1.0,
        )
        for key, _session in pairs:
            self.sessions.pop(key, None)
            self.gate.release()


async def checkpoint() -> None:
    events: list[str] = []
    supervisor = LocalSupervisor(max_sessions=1, events=events)
    authorized = Request(authorization_header=f"Bearer {TOKEN}")

    outcome, session = await supervisor.connect("missing", Request())
    assert (outcome, session, supervisor.created) == ("auth:missing", None, 0)
    print("PASS auth: missing bearer token rejected before session creation")

    outcome, first = await supervisor.connect("caller-1", authorized)
    assert outcome == "accepted" and first is not None
    outcome, extra = await supervisor.connect("caller-extra", authorized)
    assert (outcome, extra, supervisor.created) == ("capacity", None, 1)
    print("PASS capacity: extra caller rejected instead of queued")

    await supervisor.disconnect("caller-1")
    outcome, second = await supervisor.connect("caller-2", authorized)
    assert outcome == "accepted" and second is not None and second is not first
    print("PASS isolation: released slot created fresh session 2")

    supervisor.gate.start_draining()
    outcome, draining = await supervisor.connect("caller-late", authorized)
    assert (outcome, draining) == ("draining", None)
    await supervisor.shutdown()
    assert supervisor.gate.active_count == supervisor.gate.reserved_count == 0
    print("PASS shutdown: draining rejected new work and stopped session 2")

    try:
        enforce_bind_guard("0.0.0.0", auth=None)
    except ValueError as exc:
        assert "without a token" in str(exc)
    else:
        raise AssertionError("a public unauthenticated bind was accepted")
    print("PASS bind guard: public unauthenticated endpoint failed closed")

    assert events == [
        "session 1 started",
        "session 1 stopped graceful",
        "session 2 started",
        "session 2 stopped graceful",
    ]


def main() -> None:
    asyncio.run(checkpoint())


if __name__ == "__main__":
    main()
