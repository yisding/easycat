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
from collections.abc import Awaitable, Callable, Hashable, Iterable
from functools import partial
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast, overload

from easycat._numeric import is_finite_number

if TYPE_CHECKING:
    from easycat.session import Session

KeyT = TypeVar("KeyT", bound=Hashable)
ConnectionT = TypeVar("ConnectionT")
SessionT = TypeVar("SessionT")
logger = logging.getLogger(__name__)


def _validate_timeout(name: str, value: object, *, allow_none: bool = False) -> None:
    if allow_none and value is None:
        return
    if not is_finite_number(value) or value < 0:
        raise ValueError(f"{name} must be a finite number >= 0")


def _validate_poll_interval(value: object) -> None:
    if not is_finite_number(value) or value <= 0:
        raise ValueError("poll_interval_s must be a finite number > 0")


async def close_websocket_connections(
    connections: Iterable[object],
    *,
    timeout_s: float | None,
    code: int = 1001,
    reason: str = "Server shutdown after drain",
) -> None:
    """Close surviving WebSocket connections after their session drain.

    ``websockets.Server.close(close_connections=False)`` stops accepting but
    intentionally leaves established connections open. Calling ``close()``
    again cannot switch that mode because the method is idempotent, so servers
    must retain the accepted connection objects and close survivors explicitly
    after the graceful session window.
    """
    close_tasks: list[asyncio.Task[object]] = []
    seen: set[int] = set()
    for connection in connections:
        identity = id(connection)
        if identity in seen:
            continue
        seen.add(identity)
        close = getattr(connection, "close", None)
        if close is None:
            continue
        try:
            result = close(code=code, reason=reason)
        except Exception:
            continue
        if isinstance(result, Awaitable):
            close_tasks.append(asyncio.ensure_future(result))
    if close_tasks:
        await _safe_await(
            asyncio.gather(*close_tasks, return_exceptions=True),
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
        await _safe_await(asyncio.gather(*pending, return_exceptions=True), timeout_s=timeout_s)


class WebSocketSessionRuntime(Generic[ConnectionT, SessionT]):
    """Own one raw-WebSocket server's live session and drain bookkeeping."""

    def __init__(
        self,
        *,
        manager: Any,
        max_sessions: int,
        session_factory: Callable[
            [ConnectionT],
            SessionT | None | Awaitable[SessionT | None],
        ],
        runtime_feedback: bool = False,
        capacity_reason: str = "Server is at the configured session limit",
        on_session: Callable[[SessionT], Callable[[], None] | None] | None = None,
    ) -> None:
        self.manager = manager
        self.gate: CapacityGate[int] = CapacityGate(max_sessions)
        self._session_factory = session_factory
        self._runtime_feedback = runtime_feedback
        self._capacity_reason = capacity_reason
        self._on_session = on_session
        self._sessions: dict[int, SessionT] = {}
        self._connections: dict[int, ConnectionT] = {}
        self._handler_tasks: set[asyncio.Task[object]] = set()

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
        try:
            created = self._session_factory(connection)
            session = await created if isinstance(created, Awaitable) else created
            if session is None:
                return
            # The connection must be visible to shutdown immediately, but the
            # session is not drainable until manager.add() has completed
            # Session.start(). A drain during startup closes the connection and
            # cancels this handler, leaving Session.start() to roll itself back.
            self._connections[key] = connection
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
        self.start_draining(server)
        force_deadline = await self.gate.drain(
            self._active_session_pairs,
            drain_timeout_s=max(drain_timeout_s, 0.0),
            force_after=True,
            force_timeout_s=max(force_timeout_s, 0.0),
        )
        assert force_deadline is not None
        await close_websocket_connections(
            self._connections.values(),
            timeout_s=_remaining_timeout(force_deadline),
        )
        await cancel_handler_tasks(
            self._handler_tasks,
            timeout_s=_remaining_timeout(force_deadline),
        )
        await self._bounded_cleanup(
            server.wait_closed(),  # type: ignore[attr-defined]
            timeout_s=_remaining_timeout(force_deadline),
            label="WebSocket server handlers",
        )
        await self._bounded_cleanup(
            self.manager.stop_all(),
            timeout_s=_remaining_timeout(force_deadline),
            label="WebSocket sessions",
        )
        self._sessions.clear()
        self._connections.clear()

    def _active_session_pairs(self) -> list[tuple[int, SessionT]]:
        return [
            (key, self._sessions[key]) for key in self.gate.active_keys() if key in self._sessions
        ]

    @staticmethod
    async def _bounded_cleanup(
        awaitable: Awaitable[object],
        *,
        timeout_s: float,
        label: str,
    ) -> None:
        completed = await _await_with_hard_timeout(awaitable, timeout_s=timeout_s)
        if not completed:
            logger.warning("%s did not close within force timeout %ss", label, timeout_s)


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
        if isinstance(max_sessions, bool) or not isinstance(max_sessions, int):
            raise ValueError("max_sessions must be an integer >= 1")
        if max_sessions < 1:
            raise ValueError("max_sessions must be >= 1")
        self._max_sessions = max_sessions
        # A reservation counter rather than an ``asyncio.Semaphore``: the serve
        # helpers want a non-blocking reject-when-full check (the existing
        # ``session_slots.locked()`` rejection), never a coroutine that blocks
        # until a slot frees. A plain counter expresses that intent directly and
        # keeps the gate usable from sync handler guards.
        self._reserved = 0
        self._active: set[KeyT] = set()
        self._draining = False
        self._drain_tasks: set[asyncio.Task[None]] = set()

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
        _validate_poll_interval(poll_interval_s)
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
        poll_interval_s: float = 0.05,
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
        _validate_poll_interval(poll_interval_s)
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
            task = asyncio.ensure_future(result) if isinstance(result, Awaitable) else None
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
            finish_task = asyncio.create_task(
                self._finish_drain(
                    graceful,
                    force_after=force_after,
                    force_deadline=force_deadline,
                    stop_for_key=stop_for_key,
                )
            )
            self._drain_tasks.add(finish_task)
            finish_task.add_done_callback(self._drain_tasks.discard)

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
            escalation = asyncio.create_task(
                _escalate_graceful_stop(
                    key,
                    session,
                    task,
                    force_after=force_after,
                    stop_for_key=stop_for_key,
                )
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
    cancellation_requests = current_task.cancelling() if current_task is not None else 0
    try:
        if timeout_s is not None:
            await _await_with_hard_timeout(awaitable, timeout_s=timeout_s)
        else:
            await awaitable
    except asyncio.CancelledError:
        if current_task is not None and current_task.cancelling() > cancellation_requests:
            raise
    except Exception:  # pragma: no cover - defensive teardown
        pass


_BACKGROUND_TIMEOUT_TASKS: set[asyncio.Future[object]] = set()


async def _await_with_hard_timeout(
    awaitable: Awaitable[object],
    *,
    timeout_s: float,
) -> bool:
    """Wait no longer than ``timeout_s`` without awaiting cancellation cleanup.

    ``asyncio.wait_for`` is not a hard bound: after its deadline it cancels the
    child and waits for that cancellation to finish. A teardown coroutine can
    catch cancellation and keep the caller blocked indefinitely. This helper
    instead requests cancellation, leaves any still-unfinished work owned in a
    background set, and returns immediately at the deadline. It returns
    ``True`` when the awaitable completed in time and ``False`` when it remains
    in progress.
    """
    future = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait({future}, timeout=max(timeout_s, 0.0))
    except asyncio.CancelledError:
        _track_background_timeout(future)
        raise
    if future not in done:
        future.cancel()
        _track_background_timeout(future)
        # Give cooperative cancellation one event-loop turn without waiting for
        # a coroutine that deliberately resists it.
        await asyncio.sleep(0)
        return False
    await future
    return True


def _track_background_timeout(future: asyncio.Future[object]) -> None:
    """Keep timed-out teardown work owned and consume its eventual result."""
    _BACKGROUND_TIMEOUT_TASKS.add(future)

    def finish(done: asyncio.Future[object]) -> None:
        _BACKGROUND_TIMEOUT_TASKS.discard(done)
        if not done.cancelled():
            try:
                done.exception()
            except Exception:  # pragma: no cover - defensive teardown
                pass

    future.add_done_callback(finish)
