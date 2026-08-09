"""Shared capacity + draining collaborator — the M5 net-new shared layer.

This module LIFTS the inline capacity + draining logic out of the two serve
helpers into one transport-agnostic collaborator so capacity and draining
behave IDENTICALLY across WebRTC and WebSocket. The serve helpers
(``transports/webrtc.py`` and ``transports/websocket.py``) and
:class:`~easycat.server.voice_server.VoiceServer` all delegate to it.

Lift origin (the inline state replaced by this collaborator):

* WebRTC: ``transports/webrtc.py`` — ``session_slots = asyncio.Semaphore(...)``,
  ``active_sessions: set[int]``, ``shutting_down`` bool, plus the rejection
  branches in ``handle_offer`` / ``handle_health`` and the shutdown sequence.
* WebSocket: ``transports/websocket.py`` — ``session_slots =
  asyncio.Semaphore(...)`` and the ``session_slots.locked()`` /
  ``async with session_slots`` capacity logic in ``handle_connection``.

This is NET-NEW shared code, NOT a :class:`~easycat.session_manager.SessionManager`
responsibility. ``SessionManager`` stays a bare ``add``/``remove``/``stop_all``/
``connection`` registry with no capacity and no draining state. This
collaborator holds capacity + draining ONLY; it imports no aiohttp/websockets
and is fully transport-agnostic.

There is NO unified ``ConnectionContext`` type. The per-connection seam stays a
per-transport ``Callable[[TransportT], EasyConfig | Session]`` factory.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Hashable, Iterable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast, overload

from easycat._concurrency import (
    HardTimeoutStatus,
    OwnedTask,
    OwnedTaskMetadata,
    OwnerState,
    RuntimeSupervisor,
    SurvivorRegistry,
    hard_timeout,
    start_owned,
)
from easycat._numeric import is_finite_number
from easycat.runtime._event_tasks import RuntimeTaskScope, wait_for_owned_future
from easycat.session_manager import SessionStopReport, log_session_stop_failures

if TYPE_CHECKING:
    from easycat.session import Session

KeyT = TypeVar("KeyT", bound=Hashable)
ConnectionT = TypeVar("ConnectionT")
SessionT = TypeVar("SessionT")
logger = logging.getLogger(__name__)

_CAPACITY_DRAIN_TASK = "capacity_gate_drain"
_CAPACITY_DRAIN_COHORT = "capacity-gate-drain"
_WEBSOCKET_CLEANUP_TASK = "websocket_cleanup"
_WEBSOCKET_CLEANUP_COHORT = "websocket-cleanup"
_WEBSOCKET_CLOSE_TASK = "websocket_close"
_WEBSOCKET_CLOSE_COHORT = "websocket-close"


def _validate_timeout(name: str, value: object, *, allow_none: bool = False) -> None:
    if allow_none and value is None:
        return
    if not is_finite_number(value) or value < 0:
        raise ValueError(f"{name} must be a finite number >= 0")


def _validate_poll_interval(name: str, value: object) -> None:
    if not is_finite_number(value) or value <= 0:
        raise ValueError(f"{name} must be a finite number > 0")


def _validate_max_sessions(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max_sessions must be an integer >= 1")  # noqa: TRY004 domain-specific validation error
    if value < 1:
        raise ValueError("max_sessions must be >= 1")


@dataclass(frozen=True, slots=True)
class _WebSocketCloseReport:
    failed_ids: frozenset[int] = frozenset()
    incomplete_ids: frozenset[int] = frozenset()

    @property
    def unsuccessful_ids(self) -> frozenset[int]:
        return self.failed_ids | self.incomplete_ids


def _start_websocket_close_tasks(
    connections: Iterable[object],
    *,
    task_scope: RuntimeTaskScope,
    code: int,
    reason: str,
) -> tuple[list[asyncio.Task[object]], dict[asyncio.Task[object], int], set[int]]:
    """Start or recover one owned close task per distinct connection."""
    close_tasks: list[asyncio.Task[object]] = []
    task_identities: dict[asyncio.Task[object], int] = {}
    failed_ids: set[int] = set()
    existing_tasks = {task.get_name(): task for task in task_scope.tasks()}
    seen: set[int] = set()
    for connection in connections:
        identity = id(connection)
        if identity in seen:
            continue
        seen.add(identity)
        close = getattr(connection, "close", None)
        if close is None:
            continue
        task_name = f"easycat-websocket-close-{identity}"
        existing = existing_tasks.get(task_name)
        if existing is not None:
            close_tasks.append(existing)
            task_identities[existing] = identity
            continue
        try:
            result = close(code=code, reason=reason)
        except Exception as exc:
            failed_ids.add(identity)
            logger.error(
                "WebSocket connection close %s failed",
                identity,
                exc_info=exc,
            )
            continue
        if isinstance(result, Awaitable):
            task = task_scope.create_task(
                _await_cleanup_result(result),
                task_name=task_name,
            )
            assert task is not None
            close_tasks.append(task)
            task_identities[task] = identity
    return close_tasks, task_identities, failed_ids


async def _settle_websocket_close_tasks(
    close_tasks: list[asyncio.Task[object]],
    task_identities: dict[asyncio.Task[object], int],
    failed_ids: set[int],
    *,
    timeout_s: float | None,
) -> _WebSocketCloseReport:
    """Wait for close tasks and classify failures versus incomplete cleanup."""
    if not close_tasks:
        return _WebSocketCloseReport(failed_ids=frozenset(failed_ids))
    done, pending = await asyncio.wait(
        close_tasks,
        timeout=None if timeout_s is None else max(timeout_s, 0.0),
    )
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.sleep(0)
        logger.warning(
            "WebSocket connections did not close within shutdown timeout; "
            "%s close task(s) remain unsuccessful",
            len(pending),
        )
        report_late = partial(
            _report_late_shutdown_task_result,
            "WebSocket connection close",
        )
        for task in pending:
            task.add_done_callback(report_late)
    for task in done:
        identity = task_identities[task]
        if task.cancelled():
            failed_ids.add(identity)
            logger.error(
                "WebSocket connection close task %s was cancelled",
                task.get_name(),
            )
            continue
        error = task.exception()
        if error is not None:
            failed_ids.add(identity)
            logger.error(
                "WebSocket connection close task %s failed",
                task.get_name(),
                exc_info=error,
            )
    return _WebSocketCloseReport(
        failed_ids=frozenset(failed_ids),
        incomplete_ids=frozenset(task_identities[task] for task in pending),
    )


async def close_websocket_connections(
    connections: Iterable[object],
    *,
    task_scope: RuntimeTaskScope,
    timeout_s: float | None,
    code: int = 1001,
    reason: str = "Server shutdown after drain",
) -> _WebSocketCloseReport:
    """Close surviving WebSocket connections after their session drain.

    ``websockets.Server.close(close_connections=False)`` stops accepting but
    intentionally leaves established connections open. Calling ``close()``
    again cannot switch that mode because the method is idempotent, so servers
    must retain the accepted connection objects and close survivors explicitly
    after the graceful session window.
    """
    close_tasks, task_identities, failed_ids = _start_websocket_close_tasks(
        connections,
        task_scope=task_scope,
        code=code,
        reason=reason,
    )
    return await _settle_websocket_close_tasks(
        close_tasks,
        task_identities,
        failed_ids,
        timeout_s=timeout_s,
    )


async def cancel_handler_tasks(
    tasks: Iterable[asyncio.Task[object]],
    *,
    timeout_s: float | None,
) -> None:
    """Cancel and reap connection handlers that survived transport shutdown."""
    current = asyncio.current_task()
    pending = [task for task in tasks if task is not current and not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        done, unfinished = await asyncio.wait(
            pending,
            timeout=None if timeout_s is None else max(timeout_s, 0.0),
        )
        results: list[object | BaseException] = []
        done_tasks: list[asyncio.Task[object]] = []
        for task in done:
            done_tasks.append(task)
            if task.cancelled():
                results.append(asyncio.CancelledError())
            else:
                error = task.exception()
                results.append(error if error is not None else task.result())
        _report_shutdown_task_results(
            "WebSocket handler",
            done_tasks,
            results,
            explicitly_cancelled=set(pending),
        )
        if unfinished:
            logger.warning(
                "WebSocket handlers did not stop within shutdown timeout; "
                "%s handler task(s) remain active",
                len(unfinished),
            )
            report_late = partial(
                _report_late_shutdown_task_result,
                "WebSocket handler",
            )
            for task in unfinished:
                task.add_done_callback(report_late)


def _report_shutdown_task_results(
    stage: str,
    tasks: list[asyncio.Task[object]],
    results: list[object | BaseException],
    *,
    explicitly_cancelled: set[asyncio.Task[object]],
) -> int:
    """Log unexpected shutdown failures and return their count."""
    failures = 0
    for task, result in zip(tasks, results, strict=True):
        if not isinstance(result, BaseException):
            continue
        if isinstance(result, asyncio.CancelledError) and task in explicitly_cancelled:
            continue
        failures += 1
        logger.error(
            "%s task %s failed",
            stage,
            task.get_name(),
            exc_info=result,
        )
    return failures


def _report_late_shutdown_task_result(stage: str, task: asyncio.Task[object]) -> None:
    """Report a shutdown worker that fails after its hard deadline."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error(
            "%s task %s failed",
            stage,
            task.get_name(),
            exc_info=error,
        )


class WebSocketSessionRuntime(Generic[ConnectionT, SessionT]):
    """Own one raw-WebSocket server's live session and drain bookkeeping."""

    def __init__(
        self,
        *,
        manager: Any,
        max_sessions: int,
        runtime_supervisor: RuntimeSupervisor,
        runtime_id: str,
        session_factory: Callable[
            [ConnectionT],
            SessionT | None | Awaitable[SessionT | None],
        ],
        runtime_feedback: bool = False,
        capacity_reason: str = "Server is at the configured session limit",
        on_session: Callable[[SessionT], Callable[[], None] | None] | None = None,
        survivor_capacity: int = 1,
    ) -> None:
        self.manager = manager
        self.gate: CapacityGate[int] = CapacityGate(max_sessions)
        self._survivor_registry = SurvivorRegistry(
            supervisor=runtime_supervisor,
            root_id=runtime_id,
            capacity=survivor_capacity,
        )
        self._listener_wait_attempt = 0
        self._listener_wait_owner_id: str | None = None
        self._listener_wait_owned: OwnedTask[object] | None = None
        self._listener_wait_completed = False
        self._listener_wait_terminal_state = OwnerState.OPEN
        self._listener_wait_cancel_requested = False
        self._session_factory = session_factory
        self._runtime_feedback = runtime_feedback
        self._capacity_reason = capacity_reason
        self._on_session = on_session
        self._sessions: dict[int, SessionT] = {}
        self._connections: dict[int, ConnectionT] = {}
        self._connection_cleanup_retry: dict[int, ConnectionT] = {}
        self._handler_tasks: set[asyncio.Task[object]] = set()
        self._cleanup_task_scope = RuntimeTaskScope(
            owner_label=f"{runtime_id}-cleanup",
            member_name=_WEBSOCKET_CLEANUP_TASK,
            cohort=_WEBSOCKET_CLEANUP_COHORT,
            logger=logger,
            failure_message="WebSocket runtime cleanup task failed",
            drop_if_closed=False,
        )
        self._connection_close_task_scope = RuntimeTaskScope(
            owner_label=f"{runtime_id}-connection-close",
            member_name=_WEBSOCKET_CLOSE_TASK,
            cohort=_WEBSOCKET_CLOSE_COHORT,
            logger=logger,
            failure_message="WebSocket connection close task failed",
            drop_if_closed=False,
        )

    @property
    def survivor_registry(self) -> SurvivorRegistry:
        """Return the lifecycle-root registry shared by child cleanup scopes."""
        return self._survivor_registry

    @property
    def listener_cleanup_state(self) -> OwnerState:
        """Return whether the adopted listener cleanup is observably clean."""
        if self._listener_wait_completed:
            return OwnerState.CLOSED
        if self._listener_wait_owner_id is None:
            return self._listener_wait_terminal_state
        return self._survivor_registry.owner_state(self._listener_wait_owner_id)

    @property
    def listener_cleanup_metadata(self) -> tuple[OwnedTaskMetadata, ...]:
        """Return retained listener-cleanup metadata for postmortem inspection."""
        if self._listener_wait_owner_id is None:
            return ()
        return self._survivor_registry.reservations(self._listener_wait_owner_id)

    async def handle(self, connection: ConnectionT) -> None:
        """Build and run one session, deferring teardown while draining."""
        task = asyncio.current_task()
        if task is not None:
            self._handler_tasks.add(task)
        try:
            if not self.gate.try_acquire():
                reason = "Server is draining" if self.gate.is_draining else self._capacity_reason
                await connection.close(code=1013, reason=reason)  # type: ignore[attr-defined]
                return
            await self._run_connection(connection)
        finally:
            if task is not None:
                self._handler_tasks.discard(task)

    async def _run_connection(self, connection: ConnectionT) -> None:
        key = id(connection)
        cleanup: Callable[[], None] | None = None
        # A connection can spend meaningful time in an asynchronous preflight
        # (for example Twilio authentication) before it yields a Session. It
        # still needs to be visible to drain: cancellation of its handler is
        # not enough to make every websocket implementation close the accepted
        # socket.
        self._connections[key] = connection
        try:
            created = self._session_factory(connection)
            session = await created if isinstance(created, Awaitable) else created
            if session is None:
                return
            # The session is not drainable until manager.add() has completed
            # Session.start(). A drain during startup closes the already
            # registered connection and cancels this handler, leaving
            # Session.start() to roll itself back.
            if self._on_session is not None:
                cleanup = self._on_session(session)
            if self._runtime_feedback:
                from easycat.helpers import attach_runtime_feedback

                attach_runtime_feedback(cast("Session", session))
            await self.manager.add(key, session)
            if self.gate.is_draining:
                # Shutdown began in the final scheduling window of startup.
                # Roll the newly-started session back instead of publishing it
                # after the drain snapshot has already been taken.
                await self.manager.remove(key)
                return
            self.gate.track(key)
            self._sessions[key] = session
            try:
                await connection.wait_closed()  # type: ignore[attr-defined]
            finally:
                if not self.gate.is_draining:
                    await self.manager.remove(key)
        finally:
            if cleanup is not None:
                cleanup()
            self.gate.untrack(key)
            self._sessions.pop(key, None)
            self._connections.pop(key, None)
            self.gate.release()

    def start_draining(self, server: object) -> None:
        """Reject new work and stop accepting without severing live sockets."""
        self.gate.start_draining()
        server.close(close_connections=False)  # type: ignore[attr-defined]

    async def drain(
        self,
        server: object,
        *,
        drain_timeout_s: float,
        force_timeout_s: float,
    ) -> None:
        """Drain sessions, then close surviving sockets and reap handlers."""
        listener_error: Exception | None = None
        try:
            self.start_draining(server)
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            # A raw WebSocket listener close is synchronous, but it can still
            # fail (for example from an event-loop/server implementation
            # error). Its established connections and sessions remain our
            # responsibility, so preserve the error until every downstream
            # drain stage has had a chance to run.
            listener_error = exc
        force_deadline = await self.gate.drain(
            self._active_session_pairs,
            drain_timeout_s=max(drain_timeout_s, 0.0),
            force_after=True,
            force_timeout_s=max(force_timeout_s, 0.0),
        )
        assert force_deadline is not None
        close_report = _WebSocketCloseReport()
        close_candidates = dict(self._connections)
        close_candidates.update(self._connection_cleanup_retry)
        close_candidates_by_identity = {
            id(connection): (key, connection) for key, connection in close_candidates.items()
        }
        try:
            close_report = await close_websocket_connections(
                close_candidates.values(),
                task_scope=self._connection_close_task_scope,
                timeout_s=_remaining_timeout(force_deadline),
            )
            self._connection_cleanup_retry = {
                close_candidates_by_identity[identity][0]: close_candidates_by_identity[identity][
                    1
                ]
                for identity in close_report.unsuccessful_ids
                if identity in close_candidates_by_identity
            }
        finally:
            await self._connection_close_task_scope.release_standalone_if_empty()
        await cancel_handler_tasks(
            self._handler_tasks,
            timeout_s=_remaining_timeout(force_deadline),
        )
        await self._bounded_listener_wait(
            server.wait_closed,  # type: ignore[attr-defined]
            deadline=force_deadline,
            label="WebSocket server handlers",
        )
        try:
            sweep_completed, sweep_result = await self._bounded_cleanup(
                self.manager.stop_all(),
                timeout_s=_remaining_timeout(force_deadline),
                label="WebSocket sessions",
            )
        finally:
            await self._cleanup_task_scope.release_standalone_if_empty()
        sweep_error: RuntimeError | None = None
        if isinstance(sweep_result, SessionStopReport) and log_session_stop_failures(
            sweep_result,
            context="WebSocket session shutdown",
            log=logger,
        ):
            sweep_error = RuntimeError(
                f"WebSocket session shutdown retained {len(sweep_result.failures)} session(s)"
            )
        # Preserve these ownership ledgers when cancellation or an unexpected
        # cleanup error aborts the sequence or the manager retains a failed
        # session. A later drain can then retry the manager-owned stop and still
        # close the established sockets/reap their handlers.
        if sweep_completed and sweep_error is None:
            self._sessions.clear()
            self._connections.clear()
            self._connections.update(self._connection_cleanup_retry)
        if listener_error is not None:
            raise listener_error
        if sweep_error is not None:
            raise sweep_error
        if close_report.failed_ids:
            raise RuntimeError(
                "WebSocket connection shutdown retained "
                f"{len(close_report.failed_ids)} connection(s)"
            )

    def _active_session_pairs(self) -> list[tuple[int, SessionT]]:
        return [
            (key, self._sessions[key]) for key in self.gate.active_keys() if key in self._sessions
        ]

    async def _bounded_cleanup(
        self,
        awaitable: Awaitable[object],
        *,
        timeout_s: float,
        label: str,
    ) -> tuple[bool, object | None]:
        task = self._cleanup_task_scope.create_task(
            _await_cleanup_result(awaitable),
            task_name="easycat-websocket-runtime-cleanup",
        )
        assert task is not None
        completed = await wait_for_owned_future(task, timeout_s=timeout_s)
        if not completed:
            logger.warning("%s did not close within force timeout %ss", label, timeout_s)
            return False, None
        return True, task.result()

    async def _bounded_listener_wait(
        self,
        factory: Callable[[], Awaitable[object]],
        *,
        deadline: float,
        label: str,
    ) -> tuple[bool, object | None]:
        """Run or retry the one WS0.1b-owned listener cleanup stage."""
        if self._listener_wait_completed:
            return True, None
        while True:
            owned = self._listener_wait_owned
            if owned is None:
                if self._listener_wait_owner_id is None:
                    self._listener_wait_attempt += 1
                    self._listener_wait_terminal_state = OwnerState.OPEN
                    self._listener_wait_owner_id = (
                        f"{self._survivor_registry.root_id}:listener_wait_closed:"
                        f"{self._listener_wait_attempt}"
                    )

                async def wait_closed() -> object:
                    return await factory()

                owned = await start_owned(
                    wait_closed,
                    registry=self._survivor_registry,
                    owner_id=self._listener_wait_owner_id,
                    task_name="websocket.listener_wait_closed",
                )
                self._listener_wait_owned = owned

            try:
                outcome = await hard_timeout(owned, deadline)
            except asyncio.CancelledError:
                self._listener_wait_cancel_requested = True
                raise
            if outcome.status is not HardTimeoutStatus.COMPLETED:
                self._listener_wait_cancel_requested = True
                logger.warning(
                    "%s did not close before the force deadline (%s)",
                    label,
                    outcome.status.value,
                )
                return False, None

            owner_id = self._listener_wait_owner_id
            assert owner_id is not None
            self._survivor_registry.close_owner(owner_id)
            self._listener_wait_terminal_state = OwnerState.CLOSED
            self._listener_wait_owned = None
            self._listener_wait_owner_id = None
            expected_cancel = self._listener_wait_cancel_requested and isinstance(
                outcome.error,
                asyncio.CancelledError,
            )
            self._listener_wait_cancel_requested = False
            if expected_cancel:
                # The prior hard deadline requested this cancellation. The
                # legacy caller treated that as an incomplete cleanup rather
                # than a listener failure, so retry the factory under a fresh
                # owner attempt instead of changing exception policy.
                continue
            if outcome.error is not None:
                raise outcome.error
            self._listener_wait_completed = True
            return True, owned.task.result()


class CapacityGate(Generic[KeyT]):
    """Bounded capacity + draining state for one server process.

    Owns a :class:`asyncio.Semaphore` (the ``max_sessions`` cap), the set of
    active connection keys, and the ``draining`` flag. The public surface is
    designed so both serve helpers and :class:`VoiceServer` delegate identically:

    * :meth:`try_acquire` — reserve a slot (``False`` when draining OR full),
      mirroring the existing ``session_slots.locked()`` rejection.
    * :meth:`release` — return a reserved slot.
    * :meth:`track` / :meth:`untrack` — add/remove a connection key from the
      active set (drives ``active_count`` and the drain wait).
    * :meth:`start_draining` / :attr:`is_draining` — the draining flag.
    * :meth:`drain` — wait for the active set to empty up to a timeout, then
      escalate by force-stopping the remaining sessions.
    """

    def __init__(self, max_sessions: int) -> None:
        _validate_max_sessions(max_sessions)
        self._max_sessions = max_sessions
        # A reservation counter rather than an ``asyncio.Semaphore``: the serve
        # helpers want a non-blocking reject-when-full check (the existing
        # ``session_slots.locked()`` rejection), never a coroutine that blocks
        # until a slot frees. A plain counter expresses that intent directly and
        # keeps the gate usable from sync handler guards.
        self._reserved = 0
        self._active: set[KeyT] = set()
        self._draining = False
        self._drain_task_scope = RuntimeTaskScope(
            owner_label="capacity-gate-drain",
            member_name=_CAPACITY_DRAIN_TASK,
            cohort=_CAPACITY_DRAIN_COHORT,
            logger=logger,
            failure_message="CapacityGate drain task failed",
            drop_if_closed=False,
            release_standalone_when_idle=True,
        )
        self._drain_task_serial = 0

    # ── Capacity ─────────────────────────────────────────────────────

    @property
    def max_sessions(self) -> int:
        """The configured concurrent-session cap."""
        return self._max_sessions

    @property
    def active_count(self) -> int:
        """The number of currently-tracked active connections."""
        return len(self._active)

    @property
    def reserved_count(self) -> int:
        """The number of currently-reserved capacity slots."""
        return self._reserved

    def try_acquire(self) -> bool:
        """Reserve a capacity slot, or return ``False``.

        Returns ``False`` when the gate is draining OR all slots are reserved —
        mirroring the existing ``session_slots.locked()`` rejection in both
        serve helpers (so an over-capacity connection is rejected rather than
        blocking). On success the reservation counter is incremented; the caller
        must pair it with exactly one :meth:`release`.
        """
        if self._draining or self._reserved >= self._max_sessions:
            return False
        self._reserved += 1
        return True

    def release(self) -> None:
        """Return a previously-reserved capacity slot (never below zero)."""
        if self._reserved > 0:
            self._reserved -= 1

    # ── Active-set tracking ──────────────────────────────────────────

    def track(self, key: KeyT) -> None:
        """Add ``key`` to the active set (used to drive draining)."""
        self._active.add(key)

    def untrack(self, key: KeyT) -> None:
        """Remove ``key`` from the active set (idempotent)."""
        self._active.discard(key)

    def active_keys(self) -> tuple[KeyT, ...]:
        """Snapshot the active-connection keys."""
        return tuple(self._active)

    # ── Draining ─────────────────────────────────────────────────────

    @property
    def is_draining(self) -> bool:
        """Whether the gate is draining (rejecting new connections)."""
        return self._draining

    def start_draining(self) -> None:
        """Flip the draining flag so :meth:`try_acquire` rejects new connections."""
        self._draining = True

    def stop_draining(self) -> None:
        """Clear the draining flag so :meth:`try_acquire` accepts again.

        Used when a stopped server is reset for reuse: a drained gate must NOT
        stay in draining mode, or a restarted server would bind its listeners
        but reject every new connection as "draining" (readiness never recovers).
        """
        self._draining = False

    async def wait_drained(self, *, timeout_s: float, poll_interval_s: float = 0.05) -> bool:
        """Wait for active connections to disappear without stopping sessions."""
        _validate_timeout("timeout_s", timeout_s)
        _validate_poll_interval("poll_interval_s", poll_interval_s)
        if self.active_count == 0:
            return True
        deadline = asyncio.get_running_loop().time() + timeout_s
        while self.active_count > 0:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(poll_interval_s, remaining))
        return True

    async def drain(
        self,
        sessions_for_keys: Callable[[], Iterable[tuple[KeyT, object]]],
        *,
        drain_timeout_s: float,
        force_after: bool = True,
        force_timeout_s: float | None = None,
        stop_for_key: Callable[[KeyT, bool], object] | None = None,
    ) -> float | None:
        """Drive each active session GRACEFULLY, then force-escalate the stragglers.

        ``sessions_for_keys`` returns the ``(key, session)`` pairs that are
        active when called. The collaborator OWNS the per-session stop here —
        the ``VoiceServer`` ``/ws`` handler defers its teardown to this method
        while draining (see ``VoiceServer._teardown_ws_session``), so this is the
        single, effective stop. It does NOT route draining through
        ``SessionManager`` (which has no draining state).

        The drain owns the stop so there is one lifecycle authority per session
        during server shutdown. :meth:`~easycat.session._session.Session.stop`
        can now force-preempt an in-progress graceful stop, while session-like
        integrations with older idempotency-only semantics are still handled by
        the follow-on task cancellation:

        1. Snapshot the active ``(key, session)`` pairs and launch a graceful
           ``session.stop()`` task for each.
        2. Wait up to ``drain_timeout_s`` for every graceful stop to finish; if
           they all complete in time, the drain stayed graceful (no force).
        3. For any session whose graceful stop did NOT finish, CANCEL and reap
           the still-pending graceful task first. This also supports session-like
           integrations that retain idempotency-only stop semantics.
        4. Call ``session.stop(force=True)`` after the cancelled graceful task
           has unwound, so the force path owns backend teardown.
        5. Untrack every drained key.

        ``drain_timeout_s <= 0`` (the ``force=True`` path) collapses the grace
        window to zero so every session is force-escalated immediately.

        ``force_timeout_s`` (default ``None`` = unbounded) is one hard deadline
        for the concurrent forced phase. Work still running at the deadline
        remains owned in the background rather than making the caller wait for
        cancellation-resistant teardown.
        """
        _validate_timeout("drain_timeout_s", drain_timeout_s)
        _validate_timeout("force_timeout_s", force_timeout_s, allow_none=True)
        pairs = list(sessions_for_keys())
        if not pairs:
            return _deadline_after(force_timeout_s)

        # (1) launch the single graceful stop per active session. Servers pass a
        # keyed manager callback so handler cleanup and drain escalation share
        # one owned stop task. Direct CapacityGate users retain the session.stop
        # fallback.
        graceful: dict[KeyT, tuple[object, asyncio.Task[None] | None]] = {}
        for key, session in pairs:
            result = _call_stop(key, session, force=False, stop_for_key=stop_for_key)
            if result is None:
                # Nothing to stop; just drop it from the active set.
                self.untrack(key)
                continue
            task = (
                self._start_drain_task(
                    _await_stop_result(result),
                    stage="graceful",
                )
                if isinstance(result, Awaitable)
                else None
            )
            graceful[key] = (session, task)

        # (2) wait up to the grace window for the graceful stops to complete.
        pending_tasks = [t for _, t in graceful.values() if t is not None]
        try:
            if pending_tasks and drain_timeout_s > 0:
                await asyncio.wait(pending_tasks, timeout=max(drain_timeout_s, 0.0))
        finally:
            force_deadline = _deadline_after(force_timeout_s)
            # Teardown ownership survives cancellation of the caller running
            # drain. This prevents graceful tasks from becoming detached and
            # preserves a keyed path for later force escalation.
            finish_task = self._start_drain_task(
                self._finish_drain(
                    graceful,
                    force_after=force_after,
                    force_deadline=force_deadline,
                    stop_for_key=stop_for_key,
                ),
                stage="finish",
            )

        await asyncio.shield(finish_task)
        return force_deadline

    async def _finish_drain(
        self,
        graceful: dict[KeyT, tuple[object, asyncio.Task[None] | None]],
        *,
        force_after: bool,
        force_deadline: float | None,
        stop_for_key: Callable[[KeyT, bool], object] | None,
    ) -> None:
        """Escalate all remaining sessions concurrently under one deadline."""
        escalations: list[asyncio.Task[None]] = []
        for key, (session, task) in graceful.items():
            escalation = self._start_drain_task(
                _escalate_graceful_stop(
                    key,
                    session,
                    task,
                    force_after=force_after,
                    stop_for_key=stop_for_key,
                ),
                stage="escalate",
            )
            escalation.add_done_callback(partial(self._untrack_after_escalation, key))
            escalations.append(escalation)
        if escalations:
            await _safe_await(
                asyncio.gather(*escalations, return_exceptions=True),
                timeout_s=_remaining_timeout(force_deadline),
            )
        for key in graceful:
            self.untrack(key)

    def _untrack_after_escalation(self, key: KeyT, _task: asyncio.Task[None]) -> None:
        self.untrack(key)

    def _start_drain_task(
        self,
        coro: Coroutine[Any, Any, None],
        *,
        stage: str,
    ) -> asyncio.Task[None]:
        """Start one named drain worker under the gate's lifecycle owner."""
        self._drain_task_serial += 1
        task = self._drain_task_scope.create_task(
            coro,
            task_name=f"easycat-capacity-drain-{stage}-{self._drain_task_serial}",
        )
        assert task is not None
        return cast("asyncio.Task[None]", task)


def _deadline_after(timeout_s: float | None) -> float | None:
    if timeout_s is None:
        return None
    return asyncio.get_running_loop().time() + max(timeout_s, 0.0)


@overload
def _remaining_timeout(deadline: float) -> float: ...


@overload
def _remaining_timeout(deadline: None) -> None: ...


def _remaining_timeout(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(deadline - asyncio.get_running_loop().time(), 0.0)


def _call_stop(
    key: KeyT,
    session: object,
    *,
    force: bool,
    stop_for_key: Callable[[KeyT, bool], object] | None,
) -> object | None:
    if stop_for_key is not None:
        return stop_for_key(key, force)
    stop = getattr(session, "stop", None)
    if stop is None:
        return None
    return stop(force=force)


async def _await_stop_result(awaitable: Awaitable[object]) -> None:
    """Normalize an arbitrary session stop awaitable into an owned coroutine."""
    await awaitable


async def _await_cleanup_result(awaitable: Awaitable[object]) -> object:
    """Normalize a cleanup awaitable into the WebSocket runtime's task scope."""
    return await awaitable


async def _escalate_graceful_stop(
    key: KeyT,
    session: object,
    task: asyncio.Task[None] | None,
    *,
    force_after: bool,
    stop_for_key: Callable[[KeyT, bool], object] | None,
) -> None:
    """Reap, force-escalate, or cancel one session's graceful-stop task.

    * Graceful already completed (``task.done()``) — reap it, no escalation.
    * Still pending and ``force_after`` — cancel and reap the pending graceful
      task, then call ``stop(force=True)`` after its ``_stopping`` guard clears.
    * Still pending and NOT ``force_after`` — cancel it rather than block on a
      teardown that may never complete.

    The containing drain applies one shared hard deadline across every
    escalation, so session count does not multiply the forced-shutdown bound.
    """
    if task is not None and task.done():
        await _safe_await(task)
        return
    if task is not None:
        task.cancel()
        await _safe_await(task)
    if force_after:
        result = _call_stop(key, session, force=True, stop_for_key=stop_for_key)
        if isinstance(result, Awaitable):
            await _safe_await(result)


async def _safe_await(awaitable: Awaitable[object], *, timeout_s: float | None = None) -> None:
    """Await ``awaitable`` swallowing errors so one bad teardown cannot abort drain.

    When ``timeout_s`` is set, the await uses a hard deadline that does not wait
    for cancellation-resistant teardown.

    A :class:`asyncio.CancelledError` is swallowed ONLY when it belongs to the
    teardown awaitable being reaped — e.g. a graceful-stop task
    :func:`_escalate_graceful_stop` just cancelled, whose ``CancelledError``
    surfaces here when awaited. The drain's OWN cancellation must NOT be
    swallowed: if an outer caller cancels the task running
    :meth:`CapacityGate.drain` / :meth:`VoiceServer.stop`,
    ``current_task().cancelling()`` increases while this await is in progress
    and the cancellation is re-raised so cooperative cancellation is honored
    (the same idiom as
    :mod:`easycat.runtime.scope` and :mod:`easycat.config._telephony_wiring`).
    """
    current_task = asyncio.current_task()
    # Deliver a cancellation already pending at helper entry before recording
    # the stale-request baseline. A previously caught request leaves
    # cancelling() non-zero but does not raise at this checkpoint.
    if current_task is not None and current_task.cancelling():
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            await _discard_awaitable(awaitable)
            raise
    cancellation_requests = current_task.cancelling() if current_task is not None else 0
    if timeout_s is not None and not isinstance(awaitable, asyncio.Future):
        await _discard_awaitable(awaitable)
        raise TypeError("Timed _safe_await requires an already-owned Future or Task")
    try:
        if timeout_s is not None:
            completed = await wait_for_owned_future(
                cast("asyncio.Future[object]", awaitable),
                timeout_s=timeout_s,
            )
            if not completed:
                cast("asyncio.Future[object]", awaitable).add_done_callback(_observe_future_result)
        else:
            await awaitable
    except asyncio.CancelledError:
        if current_task is not None and current_task.cancelling() > cancellation_requests:
            raise
    except Exception:  # noqa: BLE001, S110  # pragma: no cover - defensive teardown
        pass


def _observe_future_result(future: asyncio.Future[object]) -> None:
    """Consume a late teardown wrapper result after its hard timeout."""
    if future.cancelled():
        return
    try:
        future.exception()
    except asyncio.CancelledError:
        pass


async def _discard_awaitable(awaitable: Awaitable[object]) -> None:
    """Cancel an owned future or close a coroutine that was never started."""
    if isinstance(awaitable, asyncio.Future):
        awaitable.cancel()
        await asyncio.gather(awaitable, return_exceptions=True)
    elif isinstance(awaitable, Coroutine):
        awaitable.close()
