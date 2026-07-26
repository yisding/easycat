"""Direct unit tests for :meth:`CapacityGate.drain`.

These exercise the drain collaborator in isolation (no sockets) and pin the
review-fix contract: the drain OWNS the single graceful stop, escalates to force
on timeout, and — critically — does NOT deadlock against a session that
replicates the real ``Session._stopping`` idempotency guard (where a
``stop(force=True)`` after an in-progress graceful stop is a no-op).
"""

from __future__ import annotations

import asyncio

import pytest

from easycat.server.transports import CapacityGate
from easycat.server.webrtc_routes import WebRTCRoutes
from easycat.session_manager import SessionManager
from easycat.transports._webrtc_config import WebRTCTransportConfig


class _GracefulSession:
    """A session whose graceful stop completes promptly (no force needed)."""

    def __init__(self) -> None:
        self.graceful = False
        self.forced = False

    async def stop(self, *, force: bool = False) -> None:
        if force:
            self.forced = True
            return
        self.graceful = True


class _HangUntilForceSession:
    """Graceful stop hangs until a force call releases it (no ``_stopping`` guard)."""

    def __init__(self) -> None:
        self.graceful_started = asyncio.Event()
        self.forced = False
        self._release = asyncio.Event()

    async def stop(self, *, force: bool = False) -> None:
        if force:
            self.forced = True
            self._release.set()
            return
        self.graceful_started.set()
        await self._release.wait()


class _GuardedSession:
    """Replicates the real ``Session._stopping`` guard.

    A graceful stop sets ``_stopping`` and hangs forever; a concurrent
    ``stop(force=True)`` is a NO-OP (the real early-return). The drain must
    cancel and reap the graceful task before it can enter the force path.
    """

    def __init__(self) -> None:
        self.graceful_started = asyncio.Event()
        self.force_started = asyncio.Event()
        self.force_path_ran = False
        self._stopping = False
        self._release = asyncio.Event()

    async def stop(self, *, force: bool = False) -> None:
        if self._stopping:
            return
        self._stopping = True
        try:
            if force:
                self.force_path_ran = True
                self.force_started.set()
                return
            self.graceful_started.set()
            await self._release.wait()
        finally:
            self._stopping = False


class _HangEvenInForceSession:
    """A session whose graceful AND forced stop both hang forever.

    The drain's ``force_timeout_s`` must bound the FORCED phase so even this
    pathological session cannot block the caller past the timeout (the task is
    cancelled / abandoned rather than awaited forever).
    """

    def __init__(self) -> None:
        self.graceful_started = asyncio.Event()
        self.force_started = asyncio.Event()
        self._never = asyncio.Event()

    async def stop(self, *, force: bool = False) -> None:
        if force:
            self.force_started.set()
        else:
            self.graceful_started.set()
        await self._never.wait()  # hangs in BOTH paths


class _CancellationResistantForceSession:
    """A forced stop that keeps running after cancellation is requested."""

    def __init__(self) -> None:
        self.force_started = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def stop(self, *, force: bool = False) -> None:
        if not force:
            await asyncio.Event().wait()
            return
        self.force_started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.release.wait()
        finally:
            self.finished.set()


class _ConcurrentForceSession:
    def __init__(self) -> None:
        self.force_started = asyncio.Event()
        self.release = asyncio.Event()

    async def stop(self, *, force: bool = False) -> None:
        if not force:
            await asyncio.Event().wait()
            return
        self.force_started.set()
        await self.release.wait()


class _ManagedGuardedSession(_GuardedSession):
    async def start(self) -> None:
        return


class _NeverClosedTransport:
    async def wait_closed(self) -> None:
        await asyncio.Event().wait()


def _make_gate(sessions: dict[int, object]) -> CapacityGate[int]:
    gate: CapacityGate[int] = CapacityGate(max_sessions=8)
    for key in sessions:
        assert gate.try_acquire()
        gate.track(key)
    return gate


def _pairs(sessions: dict[int, object]):
    return lambda: list(sessions.items())


async def test_drain_stays_graceful_when_stops_finish_in_window() -> None:
    sessions = {1: _GracefulSession(), 2: _GracefulSession()}
    gate = _make_gate(sessions)

    await gate.drain(_pairs(sessions), drain_timeout_s=1.0, force_after=True)

    for s in sessions.values():
        assert isinstance(s, _GracefulSession)
        assert s.graceful is True
        assert s.forced is False  # graceful completed; force never called
    assert gate.active_keys() == ()


async def test_drain_force_escalates_hung_session_after_timeout() -> None:
    s = _HangUntilForceSession()
    sessions: dict[int, object] = {1: s}
    gate = _make_gate(sessions)

    await asyncio.wait_for(
        gate.drain(_pairs(sessions), drain_timeout_s=0.05, force_after=True),
        timeout=2,
    )

    assert s.graceful_started.is_set()
    assert s.forced is True
    assert gate.active_keys() == ()


async def test_drain_does_not_deadlock_against_real_stopping_guard() -> None:
    # The regression: a session that mirrors the real guard. The drain starts the
    # single graceful stop (hangs), times out, cancels it so the guard clears,
    # and then enters the force path.
    s = _GuardedSession()
    sessions: dict[int, object] = {1: s}
    gate = _make_gate(sessions)

    await asyncio.wait_for(
        gate.drain(_pairs(sessions), drain_timeout_s=0.05, force_after=True),
        timeout=2,
    )

    assert s.graceful_started.is_set()
    assert s.force_path_ran is True
    assert gate.active_keys() == ()


async def test_drain_zero_timeout_force_escalates_immediately() -> None:
    s = _HangUntilForceSession()
    sessions: dict[int, object] = {1: s}
    gate = _make_gate(sessions)

    # ``drain_timeout_s <= 0`` (the ``stop(force=True)`` path) collapses the
    # grace window: the session is force-escalated without waiting.
    await asyncio.wait_for(
        gate.drain(_pairs(sessions), drain_timeout_s=0.0, force_after=True),
        timeout=2,
    )

    assert s.forced is True
    assert gate.active_keys() == ()


async def test_wait_drained_observes_natural_untrack_before_timeout() -> None:
    gate: CapacityGate[int] = CapacityGate(max_sessions=2)
    assert gate.try_acquire()
    gate.track(1)

    async def release_later() -> None:
        await asyncio.sleep(0.01)
        gate.untrack(1)
        gate.release()

    task = asyncio.create_task(release_later())
    try:
        assert await gate.wait_drained(timeout_s=1.0)
    finally:
        await task
    assert gate.active_keys() == ()
    assert gate.reserved_count == 0


async def test_wait_drained_returns_false_when_active_session_survives_timeout() -> None:
    gate: CapacityGate[int] = CapacityGate(max_sessions=2)
    assert gate.try_acquire()
    gate.track(1)

    assert await gate.wait_drained(timeout_s=0.0) is False
    assert gate.active_keys() == (1,)
    assert gate.reserved_count == 1


async def test_drain_force_timeout_bounds_a_hung_force_stop() -> None:
    # F4: a session whose force-stop ALSO hangs must not block the drain past
    # ~force_timeout_s. The forced phase (the stop(force=True) call and the
    # follow-on cancel-await) is bounded; the hung task is abandoned.
    s = _HangEvenInForceSession()
    sessions: dict[int, object] = {1: s}
    gate = _make_gate(sessions)

    loop = asyncio.get_running_loop()
    start = loop.time()
    await asyncio.wait_for(
        gate.drain(
            _pairs(sessions),
            drain_timeout_s=0.05,
            force_after=True,
            force_timeout_s=0.1,
        ),
        timeout=2,
    )
    elapsed = loop.time() - start

    assert s.graceful_started.is_set()
    assert s.force_started.is_set()
    # Drain returned promptly despite the force-stop hanging (bounded, not 2s).
    assert elapsed < 1.5
    assert gate.active_keys() == ()


async def test_force_timeout_is_hard_when_stop_resists_cancellation() -> None:
    session = _CancellationResistantForceSession()
    sessions: dict[int, object] = {1: session}
    gate = _make_gate(sessions)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await gate.drain(
        _pairs(sessions),
        drain_timeout_s=0.0,
        force_after=True,
        force_timeout_s=0.05,
    )
    elapsed = loop.time() - started

    assert elapsed < 0.5
    assert session.force_started.is_set()
    assert session.cancel_seen.is_set()
    assert gate.active_keys() == ()

    session.release.set()
    await asyncio.wait_for(session.finished.wait(), timeout=1)
    await asyncio.sleep(0)


async def test_forced_shutdown_runs_all_sessions_concurrently() -> None:
    sessions = {key: _ConcurrentForceSession() for key in range(6)}
    gate = _make_gate(sessions)

    drain_task = asyncio.create_task(
        gate.drain(
            _pairs(sessions),
            drain_timeout_s=0.0,
            force_after=True,
            force_timeout_s=1.0,
        )
    )
    await asyncio.wait_for(
        asyncio.gather(*(session.force_started.wait() for session in sessions.values())),
        timeout=0.2,
    )
    for session in sessions.values():
        session.release.set()
    await asyncio.wait_for(drain_task, timeout=1)

    assert gate.active_keys() == ()


async def test_cancelled_drain_keeps_teardown_owned_and_force_escalates() -> None:
    session = _GuardedSession()
    sessions: dict[int, object] = {1: session}
    gate = _make_gate(sessions)

    drain_task = asyncio.create_task(
        gate.drain(
            _pairs(sessions),
            drain_timeout_s=30.0,
            force_after=True,
            force_timeout_s=1.0,
        )
    )
    await session.graceful_started.wait()
    drain_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drain_task

    await asyncio.wait_for(session.force_started.wait(), timeout=1)
    await asyncio.gather(*list(gate._drain_tasks))
    assert gate.active_keys() == ()


async def test_webrtc_cleanup_and_drain_share_one_force_escalatable_stop() -> None:
    session = _ManagedGuardedSession()
    manager: SessionManager[int] = SessionManager()
    await manager.add(1, session)  # type: ignore[arg-type]
    gate = _make_gate({1: session})
    active: dict[int, object] = {1: session}
    routes = WebRTCRoutes(
        WebRTCTransportConfig(static_dir=None),
        auth=None,
        config_factory=lambda _transport: session,  # type: ignore[arg-type]
        gate=gate,
        manager=manager,
        runtime_feedback=False,
        active_session_objs=active,
    )
    cleanup = asyncio.create_task(
        routes._cleanup_session(1, _NeverClosedTransport())  # type: ignore[arg-type]
    )
    routes._cleanup_tasks.add(cleanup)
    routes._cleanup_task_keys[cleanup] = 1
    cleanup.add_done_callback(routes._cleanup_task_done)

    gate.start_draining()
    drain = asyncio.create_task(
        gate.drain(
            lambda: [(1, session)],
            drain_timeout_s=0.02,
            force_after=True,
            force_timeout_s=None,
            stop_for_key=routes._stop_managed_session,
        )
    )
    await session.graceful_started.wait()
    cleanup_cancel = asyncio.create_task(routes.cancel_cleanup_tasks())
    await asyncio.wait_for(asyncio.gather(drain, cleanup_cancel), timeout=2)

    assert session.force_path_ran is True
    assert manager.get(1) is None
    assert active == {}
    assert gate.active_keys() == ()
    assert gate.reserved_count == 0
    assert routes._cleanup_tasks == set()


async def test_drain_with_no_active_sessions_is_a_noop() -> None:
    gate: CapacityGate[int] = CapacityGate(max_sessions=4)
    await gate.drain(lambda: [], drain_timeout_s=1.0, force_after=True)
    assert gate.active_keys() == ()


async def test_drain_without_force_cancels_pending_graceful_stop() -> None:
    # With ``force_after=False`` a still-pending graceful stop must be cancelled,
    # not awaited forever, so the drain still returns.
    s = _HangUntilForceSession()
    sessions: dict[int, object] = {1: s}
    gate = _make_gate(sessions)

    await asyncio.wait_for(
        gate.drain(_pairs(sessions), drain_timeout_s=0.05, force_after=False),
        timeout=2,
    )

    assert s.graceful_started.is_set()
    assert s.forced is False  # no force escalation requested
    assert gate.active_keys() == ()
