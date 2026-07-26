from __future__ import annotations

import asyncio

import pytest

from easycat.session import Session
from easycat.session_manager import SessionManager
from tests.session._session_core_helpers import FakeTransport, _full_config


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


@pytest.mark.parametrize("release_method", ["remove", "stop_all"])
@pytest.mark.asyncio
async def test_cancelled_add_does_not_remove_replacement_session(
    release_method: str,
) -> None:
    manager: SessionManager[str] = SessionManager()
    start_entered = asyncio.Event()

    class BlockingSession(_DummySession):
        async def start(self) -> None:
            self.started += 1
            start_entered.set()
            await asyncio.Event().wait()

    original = BlockingSession()
    add_task = asyncio.create_task(manager.add("reused", original))  # type: ignore[arg-type]
    await start_entered.wait()

    if release_method == "remove":
        await manager.remove("reused")
    else:
        await manager.stop_all()

    replacement = _DummySession()
    await manager.add("reused", replacement)  # type: ignore[arg-type]
    add_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await add_task

    assert manager.get("reused") is replacement
    assert original.stopped == 1
    await manager.remove("reused")
    assert replacement.stopped == 1


@pytest.mark.asyncio
async def test_cancelled_real_session_start_rolls_back_before_manager_untracks() -> None:
    manager: SessionManager[str] = SessionManager()

    class BlockingWarmupTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.warmup_entered = asyncio.Event()
            self.disconnect_entered = asyncio.Event()
            self.allow_disconnect = asyncio.Event()

        async def warmup(self) -> None:
            self.warmup_entered.set()
            await asyncio.Event().wait()

        async def disconnect(self) -> None:
            self.disconnect_entered.set()
            await self.allow_disconnect.wait()
            await super().disconnect()

    transport = BlockingWarmupTransport()
    session = Session(_full_config(transport=transport))
    add_task = asyncio.create_task(manager.add("cancelled", session))

    await transport.warmup_entered.wait()
    assert transport.connected
    add_task.cancel()

    await transport.disconnect_entered.wait()
    assert manager.get("cancelled") is session

    # Repeated cancellation must not interrupt the rollback already in
    # progress or let the manager untrack the session before disconnect.
    add_task.cancel()
    await asyncio.sleep(0)
    assert not add_task.done()
    assert manager.get("cancelled") is session
    transport.allow_disconnect.set()

    with pytest.raises(asyncio.CancelledError):
        await add_task

    assert manager.get("cancelled") is None
    assert not session.is_running
    assert transport.disconnected
    assert session._health_checkers == []
    assert session._audio_router.pipeline_task is None
    assert session._audio_router.outbound_task is None


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
async def test_cancelled_remove_retains_session_for_force_sweep() -> None:
    manager: SessionManager[str] = SessionManager()

    class BlockingSession:
        def __init__(self) -> None:
            self.started = 0
            self.graceful_started = asyncio.Event()
            self.forced = False

        async def start(self) -> None:
            self.started += 1

        async def stop(self, *, force: bool = False) -> None:
            if force:
                self.forced = True
                return
            self.graceful_started.set()
            await asyncio.Event().wait()

    session = BlockingSession()
    await manager.add("call", session)  # type: ignore[arg-type]
    remove_task = asyncio.create_task(manager.remove("call"))
    await session.graceful_started.wait()

    remove_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await remove_task

    assert manager.get("call") is session
    await manager.stop_all(force=True)
    assert session.forced is True
    assert manager.get("call") is None


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
