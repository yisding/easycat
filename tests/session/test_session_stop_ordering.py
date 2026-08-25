"""Partial-order contracts for the current ``Session.stop()`` choreography."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from easycat._epoch import Lease
from easycat._turn_context import TurnContext
from easycat.session._session import Session
from easycat.session.actions import SessionAction, SessionActionResult
from tests.session._session_core_helpers import (
    FakeSTT,
    FakeTransport,
    FakeTTS,
    FakeVAD,
    TrackingJournal,
    _full_config,
)


class _OrderingTransport(FakeTransport):
    def __init__(self, record: Callable[[str], None]) -> None:
        super().__init__()
        self._record = record

    async def disconnect(self) -> None:
        self._record("transport.disconnect")
        await super().disconnect()


class _OrderingSTT(FakeSTT):
    def __init__(self, record: Callable[[str], None]) -> None:
        super().__init__()
        self._record = record

    def close(self) -> None:
        self._record("provider.stt.close")


class _OrderingTTS(FakeTTS):
    def __init__(self, record: Callable[[str], None]) -> None:
        super().__init__()
        self._record = record

    async def aclose(self) -> None:
        self._record("provider.tts.close")


class _OrderingVAD(FakeVAD):
    def __init__(self, record: Callable[[str], None]) -> None:
        super().__init__()
        self._record = record

    def close(self) -> None:
        self._record("provider.vad.close")


class _OrderingAgent:
    def __init__(self, record: Callable[[str], None]) -> None:
        self._record = record

    async def run(self, text: str) -> str:
        return text

    async def aclose(self) -> None:
        self._record("agent.close")


class _OrderingJournal(TrackingJournal):
    def __init__(self, record: Callable[[str], None]) -> None:
        super().__init__()
        self._record = record

    def finalize(self) -> None:
        self._record("journal.finalize")
        super().finalize()

    def close(self) -> None:
        self._record("journal.close")
        super().close()


class _OrderingHealthChecker:
    def __init__(self, record: Callable[[str], None]) -> None:
        self._record = record

    async def stop(self) -> None:
        self._record("health.stop")


class _OrderingTurnLifecycle:
    def __init__(self, wrapped: Any, record: Callable[[str], None]) -> None:
        self._wrapped = wrapped
        self._record = record

    @property
    def current(self) -> TurnContext | None:
        return self._wrapped.current

    def clear_identity(self) -> Lease[TurnContext | None]:
        self._record("identity.clear")
        return self._wrapped.clear_identity()


def _assert_before(events: list[str], first: str, second: str) -> None:
    assert first in events, f"missing ordering observation: {first}\n{events}"
    assert second in events, f"missing ordering observation: {second}\n{events}"
    assert events.index(first) < events.index(second), (
        f"expected {first!r} before {second!r}\n{events}"
    )


@pytest.mark.asyncio
async def test_force_stop_signals_entire_task_cohort_before_awaiting_a_member() -> None:
    """Force stop is a broadcast barrier, not cancel-one/await-one sequencing."""
    session = Session(_full_config())
    members: dict[str, asyncio.Task[None]] = {}
    snapshots: list[tuple[str, frozenset[str]]] = []
    blocker = asyncio.Event()

    async def cohort_member(name: str) -> None:
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            snapshots.append(
                (
                    name,
                    frozenset(
                        member_name for member_name, task in members.items() if task.cancelling()
                    ),
                )
            )
            raise

    members["pipeline"] = asyncio.create_task(cohort_member("pipeline"))
    members["tts"] = asyncio.create_task(cohort_member("tts"))
    members["outbound"] = asyncio.create_task(cohort_member("outbound"))
    members["scope"] = session._runtime_scope.create_task(
        "stop_ordering_scope_member",
        cohort_member("scope"),
    )
    session._audio_router._pipeline_task = members["pipeline"]
    session._tts_scheduler.active_turn_task = members["tts"]
    session._audio_router._outbound_task = members["outbound"]
    await asyncio.sleep(0)

    await session.stop(force=True)

    expected = frozenset(members)
    assert {name for name, _signalled in snapshots} == expected
    assert all(signalled == expected for _name, signalled in snapshots)
    assert all(task.cancelled() for task in members.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_stop_preserves_reviewed_partial_order(  # noqa: C901, PLR0915
    force: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    record = events.append
    journal = _OrderingJournal(record)
    session = Session(
        _full_config(
            agent=_OrderingAgent(record),
            journal=journal,
            stt=_OrderingSTT(record),
            transport=_OrderingTransport(record),
            tts=_OrderingTTS(record),
            vad=_OrderingVAD(record),
        )
    )

    async def record_async(name: str) -> None:
        record(name)

    async def scope_drain(name: str) -> None:
        record(f"scope.drain.{name}")

    async def scope_cancel_and_drain(name: str | None = None) -> None:
        record("scope.drain.all" if name is None else f"scope.drain.{name}")

    original_signal_cohort = session._runtime_scope.signal_cohort

    def scope_signal_cohort(
        cohort: str,
        *,
        force: bool,
        _exclude_tasks: set[asyncio.Task[Any]] | None = None,
    ):
        record("scope.signal")
        return original_signal_cohort(
            cohort,
            force=force,
            _exclude_tasks=_exclude_tasks,
        )

    async def scope_drain_cohort(
        signal: object,
        *,
        force: bool | None = None,
    ) -> None:
        if isinstance(signal, str):
            assert force is False
            record(f"scope.drain.{signal}")
        else:
            assert force is None
            record("scope.drain.all")

    monkeypatch.setattr(session._runtime_scope, "drain", scope_drain)
    monkeypatch.setattr(
        session._runtime_scope,
        "cancel_and_drain",
        scope_cancel_and_drain,
    )
    monkeypatch.setattr(
        session._runtime_scope,
        "cohorts",
        lambda *, force: ("default",) if force else (),
    )
    monkeypatch.setattr(session._runtime_scope, "signal_cohort", scope_signal_cohort)
    monkeypatch.setattr(session._runtime_scope, "drain_cohort", scope_drain_cohort)
    monkeypatch.setattr(
        session._greeting,
        "cancel",
        lambda: record_async("greeting.cancel"),
    )
    monkeypatch.setattr(
        session._stt_committer,
        "cancel",
        lambda _turn: record_async("stt.cancel"),
    )
    monkeypatch.setattr(
        session._tts_scheduler,
        "cancel",
        lambda: record_async("tts.cancel"),
    )
    monkeypatch.setattr(
        session._audio_router,
        "stop_ingress",
        lambda: record_async("ingress.stop"),
    )
    expected_force = force

    async def stop_outbound(*, force: bool = False) -> None:
        assert force is expected_force
        await record_async("outbound.stop")

    monkeypatch.setattr(
        session._audio_router,
        "stop_outbound",
        stop_outbound,
    )

    original_helpers_stop = session._stop_helpers
    original_queue_close = session._outbound_queue.close
    original_manager_shutdown = session._turn_manager.shutdown
    original_mark_closed = session._mark_closed

    def stop_helpers() -> None:
        record("helpers.stop")
        original_helpers_stop()

    def close_queue() -> None:
        record("queue.close")
        original_queue_close()

    async def shutdown_manager() -> None:
        record("manager.shutdown")
        await original_manager_shutdown()

    def mark_closed() -> None:
        record("session.closed")
        original_mark_closed()

    monkeypatch.setattr(session, "_stop_helpers", stop_helpers)
    monkeypatch.setattr(session._outbound_queue, "close", close_queue)
    monkeypatch.setattr(session._turn_manager, "shutdown", shutdown_manager)
    monkeypatch.setattr(session, "_mark_closed", mark_closed)
    session._health_checkers = [_OrderingHealthChecker(record)]  # type: ignore[list-item]
    session._turn_lifecycle = _OrderingTurnLifecycle(session._turn_lifecycle, record)  # type: ignore[assignment]

    await session.stop(force=force)

    branch_edges: tuple[tuple[str, str], ...]
    health_edges: tuple[tuple[str, str], ...]
    if force:
        branch_edges = (
            ("scope.signal", "stt.cancel"),
            ("stt.cancel", "scope.drain.all"),
            ("scope.drain.all", "ingress.stop"),
        )
        health_edges = (("health.stop", "helpers.stop"),)
    else:
        branch_edges = (
            ("scope.drain.barge_in_cleanup", "greeting.cancel"),
            ("greeting.cancel", "stt.cancel"),
            ("stt.cancel", "tts.cancel"),
            ("tts.cancel", "ingress.stop"),
        )
        health_edges = (
            ("health.stop", "scope.drain.supervisor-streams"),
            ("scope.drain.supervisor-streams", "helpers.stop"),
        )

    common_edges = (
        ("ingress.stop", "health.stop"),
        ("helpers.stop", "queue.close"),
        ("queue.close", "outbound.stop"),
        ("outbound.stop", "scope.drain.pipeline_heartbeat"),
        ("scope.drain.pipeline_heartbeat", "transport.disconnect"),
        ("transport.disconnect", "manager.shutdown"),
        ("manager.shutdown", "agent.close"),
        ("identity.clear", "journal.finalize"),
        ("journal.finalize", "journal.close"),
        ("journal.close", "session.closed"),
    )
    for first, second in (*branch_edges, *health_edges, *common_edges):
        _assert_before(events, first, second)

    # Audio-provider siblings share one finalizer node. Their internal order
    # is intentionally unconstrained; only the surrounding graph edges matter.
    for provider_close in (
        "provider.stt.close",
        "provider.tts.close",
        "provider.vad.close",
    ):
        _assert_before(events, "agent.close", provider_close)
        _assert_before(events, provider_close, "identity.clear")


class _RecordingExecutor:
    """Minimal action executor double that records its close lifecycle."""

    def __init__(
        self,
        record: Callable[[str], None],
        *,
        close_error: Exception | None = None,
    ) -> None:
        self._record = record
        self._close_error = close_error
        self.close_count = 0

    def supports(self, action: SessionAction) -> bool:
        return False

    async def execute(
        self,
        session: Any,
        action: SessionAction,
    ) -> SessionActionResult:
        raise AssertionError("executor should not execute during teardown")

    async def close(self) -> None:
        self.close_count += 1
        self._record(f"executor.close:{id(self)}")
        if self._close_error is not None:
            raise self._close_error


class _FailingCloseAgent:
    async def run(self, text: str) -> str:
        return text

    async def aclose(self) -> None:
        raise RuntimeError("agent close boom")


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_stop_closes_registered_action_executor_once(force: bool) -> None:
    executor = _RecordingExecutor(lambda name: None)
    session = Session(_full_config())
    session.register_action_executor(executor)

    await session.stop(force=force)

    assert executor.close_count == 1


@pytest.mark.asyncio
async def test_agent_close_failure_does_not_skip_executor_close() -> None:
    executor = _RecordingExecutor(lambda name: None)
    session = Session(_full_config(agent=_FailingCloseAgent()))
    session.register_action_executor(executor)

    with pytest.raises(RuntimeError, match="agent close boom"):
        await session.stop()

    assert executor.close_count == 1


@pytest.mark.asyncio
async def test_first_executor_close_failure_still_closes_remaining_executors() -> None:
    failing = _RecordingExecutor(lambda name: None, close_error=RuntimeError("boom"))
    healthy = _RecordingExecutor(lambda name: None)
    session = Session(_full_config())
    session.register_action_executor(healthy)
    session.register_action_executor(failing)

    with pytest.raises(RuntimeError, match="boom"):
        await session.stop()

    assert failing.close_count == 1
    assert healthy.close_count == 1


@pytest.mark.asyncio
async def test_both_agent_and_executor_close_failures_are_preserved() -> None:
    failing = _RecordingExecutor(lambda name: None, close_error=RuntimeError("executor boom"))
    session = Session(_full_config(agent=_FailingCloseAgent()))
    session.register_action_executor(failing)

    with pytest.raises(ExceptionGroup, match="runtime resource cleanup") as excinfo:
        await session.stop()

    assert [str(exc) for exc in excinfo.value.exceptions] == [
        "agent close boom",
        "executor boom",
    ]
    assert failing.close_count == 1
