from __future__ import annotations

import asyncio

import pytest

from easycat.session_manager import SessionManager


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
async def test_session_manager_releases_key_when_start_fails() -> None:
    manager: SessionManager[str] = SessionManager()

    class FailingSession(_DummySession):
        async def start(self) -> None:
            self.started += 1
            raise RuntimeError("start failed")

    failed = FailingSession()
    with pytest.raises(RuntimeError, match="start failed"):
        await manager.add("reusable", failed)  # type: ignore[arg-type]

    assert manager.get("reusable") is None
    assert failed.started == 1
    assert failed.stopped == 0

    replacement = _DummySession()
    await manager.add("reusable", replacement)  # type: ignore[arg-type]
    assert manager.get("reusable") is replacement
    await manager.remove("reusable")
    assert replacement.stopped == 1


@pytest.mark.asyncio
async def test_session_manager_releases_key_when_start_is_cancelled() -> None:
    manager: SessionManager[str] = SessionManager()
    start_entered = asyncio.Event()

    class BlockingSession(_DummySession):
        async def start(self) -> None:
            self.started += 1
            start_entered.set()
            await asyncio.Event().wait()

    cancelled = BlockingSession()
    add_task = asyncio.create_task(manager.add("reusable", cancelled))  # type: ignore[arg-type]
    await start_entered.wait()
    add_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await add_task

    assert manager.get("reusable") is None
    assert cancelled.started == 1
    assert cancelled.stopped == 0

    replacement = _DummySession()
    await manager.add("reusable", replacement)  # type: ignore[arg-type]
    assert manager.get("reusable") is replacement
    await manager.remove("reusable")
    assert replacement.stopped == 1


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


@pytest.mark.asyncio
async def test_session_manager_connection_can_attach_runtime_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager: SessionManager[str] = SessionManager()
    session = _DummySession()
    attached: list[object] = []

    monkeypatch.setattr("easycat.helpers.attach_runtime_feedback", attached.append)

    async with manager.connection("a", session, runtime_feedback=True):
        assert manager.get("a") is session
        assert session.started == 1

    assert attached == [session]
    assert manager.get("a") is None
    assert session.stopped == 1


@pytest.mark.asyncio
async def test_session_manager_connection_leaves_feedback_off_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager: SessionManager[str] = SessionManager()
    session = _DummySession()
    attached: list[object] = []

    monkeypatch.setattr("easycat.helpers.attach_runtime_feedback", attached.append)

    async with manager.connection("a", session):
        assert manager.get("a") is session

    assert attached == []
