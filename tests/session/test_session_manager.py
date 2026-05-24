from __future__ import annotations

import pytest

from easycat._session_manager import SessionManager


class _DummySession:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


@pytest.mark.asyncio
async def test_session_manager_add_remove() -> None:
    manager: SessionManager[str] = SessionManager()
    session = _DummySession()

    await manager.add("a", session)  # type: ignore[arg-type]

    assert manager.get("a") is session
    assert session.started == 1

    await manager.remove("a")
    assert manager.get("a") is None
    assert session.stopped == 1


@pytest.mark.asyncio
async def test_session_manager_stop_all() -> None:
    manager: SessionManager[str] = SessionManager()
    a = _DummySession()
    b = _DummySession()

    await manager.add("a", a)  # type: ignore[arg-type]
    await manager.add("b", b)  # type: ignore[arg-type]

    await manager.stop_all()

    assert manager.get("a") is None
    assert manager.get("b") is None
    assert a.stopped == 1
    assert b.stopped == 1
