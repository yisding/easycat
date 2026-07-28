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
from typing import Any, Generic, TypeVar

KeyT = TypeVar("KeyT", bound=Hashable)
logger = logging.getLogger(__name__)


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


class WebSocketSessionRuntime:
    """Own one raw-WebSocket server's live session and drain bookkeeping."""

    def __init__(
        self,
        *,
        manager: Any,
        max_sessions: int,
        session_factory: Callable[[object], object],
        runtime_feedback: bool = False,
        capacity_reason: str = "Server is at the configured session limit",
        on_session: Callable[[object], Callable[[], None] | None] | None = None,
    ) -> None:
        self.manager = manager
        self.gate: CapacityGate[int] = CapacityGate(max_sessions)
        self._session_factory = session_factory
        self._runtime_feedback = runtime_feedback
        self._capacity_reason = capacity_reason
        self._on_session = on_session
        self._sessions: dict[int, object] = {}
        self._connections: dict[int, object] = {}
        self._handler_tasks: set[asyncio.Task[object]] = set()

    async def handle(self, connection: object) -> None:
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

    async def _run_connection(self, connection: object) -> None:
        key = id(connection)
        cleanup: Callable[[], None] | None = None
        try:
            session = self._session_factory(connection)
            if self._on_session is not None:
                cleanup = self._on_session(session)
            self.gate.track(key)
            self._sessions[key] = session
            self._connections[key] = connection
            if self._runtime_feedback:
                from easycat.helpers import attach_runtime_feedback

                attach_runtime_feedback(session)
            await self.manager.add(key, session)
            try:
                await connection.wait_closed()  # type: ignore[attr-defined]
            finally:
                if not self.gate.is_draining:
                    await self.manager.remove(key)
        finally:
            if cleanup is not None:
                cleanup()
            if not self.gate.is_draining:
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
        await self.gate.drain(
            self._active_session_pairs,
            drain_timeout_s=max(drain_timeout_s, 0.0),
            force_after=True,
            force_timeout_s=max(force_timeout_s, 0.0),
        )
        await close_websocket_connections(
            self._connections.values(),
            timeout_s=max(force_timeout_s, 0.0),
        )
        await cancel_handler_tasks(
            self._handler_tasks,
            timeout_s=max(force_timeout_s, 0.0),
        )
        await self._bounded_cleanup(
            server.wait_closed(),  # type: ignore[attr-defined]
            timeout_s=max(force_timeout_s, 0.0),
            label="WebSocket server handlers",
        )
        await self._bounded_cleanup(
            self.manager.stop_all(),
            timeout_s=max(force_timeout_s, 0.0),
            label="WebSocket sessions",
        )
        self._sessions.clear()
        self._connections.clear()

    def _active_session_pairs(self) -> list[tuple[int, object]]:
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
        try:
            await asyncio.wait_for(awaitable, timeout=timeout_s)
        except TimeoutError:
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

    async def drain(
        self,
        sessions_for_keys: Callable[[], Iterable[tuple[KeyT, object]]],
        *,
        drain_timeout_s: float,
        force_after: bool = True,
        force_timeout_s: float | None = None,
        poll_interval_s: float = 0.05,
    ) -> None:
        """Drive each active session GRACEFULLY, then force-escalate the stragglers.

        ``sessions_for_keys`` returns the ``(key, session)`` pairs that are
        active when called. The collaborator OWNS the per-session stop here —
        the ``VoiceServer`` ``/ws`` handler defers its teardown to this method
        while draining (see ``VoiceServer._teardown_ws_session``), so this is the
        single, effective stop. It does NOT route draining through
        ``SessionManager`` (which has no draining state).

        Why the drain owns the stop (review fix): the real
        :meth:`~easycat.session._session.Session.stop` has a ``_stopping``
        idempotency guard, so a ``force=True`` call after an in-progress graceful
        ``stop`` is a NO-OP. If the handler had already started a graceful stop
        that hung, the drain could never preempt it and ``stop()`` would deadlock.
        By making the drain start the (single) graceful stop itself, the
        force-escalation path stays effective:

        1. Snapshot the active ``(key, session)`` pairs and launch a graceful
           ``session.stop()`` task for each.
        2. Wait up to ``drain_timeout_s`` for every graceful stop to finish; if
           they all complete in time, the drain stayed graceful (no force).
        3. For any session whose graceful stop did NOT finish, call
           ``session.stop(force=True)`` (the force path runs for an
           idempotency-free session; for a real ``Session`` whose graceful stop
           is hung, the guard makes it a no-op — handled by step 4) and then
           CANCEL the still-pending graceful task so a genuinely-hung teardown
           cannot block the caller forever.
        4. Untrack every drained key.

        ``drain_timeout_s <= 0`` (the ``force=True`` path) collapses the grace
        window to zero so every session is force-escalated immediately.

        ``force_timeout_s`` (default ``None`` = unbounded) bounds the FORCED
        phase: the ``stop(force=True)`` call AND the follow-on cancel-await are
        each wrapped in :func:`asyncio.wait_for`, so a session that hangs even
        in its force-stop cannot block the caller past roughly
        ``force_timeout_s``. A hung force-stop is abandoned (the task is
        cancelled) rather than awaited forever.
        """
        pairs = list(sessions_for_keys())
        if not pairs:
            return

        # (1) launch the single graceful stop per active session. The raw
        # ``stop()`` coroutine is scheduled directly as the task so cancelling it
        # never leaves an un-awaited coroutine; a synchronous ``stop`` runs inline
        # and is recorded with no pending task (``None``).
        graceful: dict[KeyT, tuple[object, asyncio.Task[None] | None]] = {}
        for key, session in pairs:
            stop = getattr(session, "stop", None)
            if stop is None:
                # Nothing to stop; just drop it from the active set.
                self.untrack(key)
                continue
            result = stop(force=False)
            task = asyncio.ensure_future(result) if isinstance(result, Awaitable) else None
            graceful[key] = (session, task)

        # (2) wait up to the grace window for the graceful stops to complete.
        pending_tasks = [t for _, t in graceful.values() if t is not None]
        if pending_tasks and drain_timeout_s > 0:
            await asyncio.wait(pending_tasks, timeout=max(drain_timeout_s, 0.0))

        # (3) escalate / reap each graceful stop, then (4) untrack the key.
        for key, (session, task) in graceful.items():
            await _escalate_graceful_stop(
                session, task, force_after=force_after, force_timeout_s=force_timeout_s
            )
            self.untrack(key)


async def _escalate_graceful_stop(
    session: object,
    task: asyncio.Task[None] | None,
    *,
    force_after: bool,
    force_timeout_s: float | None = None,
) -> None:
    """Reap, force-escalate, or cancel one session's graceful-stop task.

    * Graceful already completed (``task.done()``) — reap it, no escalation.
    * Still pending and ``force_after`` — call ``stop(force=True)`` then cancel
      the pending graceful task so a hung teardown cannot block forever.
    * Still pending and NOT ``force_after`` — cancel it rather than block on a
      teardown that may never complete.

    ``force_timeout_s`` bounds the FORCED phase: the ``stop(force=True)`` call
    and the follow-on cancel-await are each bounded by it (``None`` = unbounded),
    so a session that hangs even in its force-stop is abandoned rather than
    blocking the drain forever.
    """
    if task is not None and task.done():
        await _safe_await(task)
        return
    if force_after:
        stop = getattr(session, "stop", None)
        if stop is not None:
            await _safe_await_stop(stop, force=True, timeout_s=force_timeout_s)
    if task is not None:
        task.cancel()
        await _safe_await(task, timeout_s=force_timeout_s)


async def _safe_await_stop(
    stop: Callable[..., object], *, force: bool, timeout_s: float | None = None
) -> None:
    """Call ``stop(force=...)`` and await it if awaitable, swallowing errors.

    When ``timeout_s`` is set the await is bounded by :func:`asyncio.wait_for`;
    a hung force-stop is cancelled (and swallowed) rather than awaited forever.
    """
    result = stop(force=force)
    if isinstance(result, Awaitable):
        await _safe_await(result, timeout_s=timeout_s)


async def _safe_await(awaitable: Awaitable[object], *, timeout_s: float | None = None) -> None:
    """Await ``awaitable`` swallowing errors so one bad teardown cannot abort drain.

    When ``timeout_s`` is set, the await is bounded by :func:`asyncio.wait_for`
    so a hung teardown cannot block the drain past roughly ``timeout_s``; the
    :class:`TimeoutError` it raises cancels the awaitable and is swallowed here.

    A :class:`asyncio.CancelledError` is swallowed ONLY when it belongs to the
    teardown awaitable being reaped — e.g. a graceful-stop task
    :func:`_escalate_graceful_stop` just cancelled, whose ``CancelledError``
    surfaces here when awaited. The drain's OWN cancellation must NOT be
    swallowed: if an outer caller cancels the task running
    :meth:`CapacityGate.drain` / :meth:`VoiceServer.stop`,
    ``current_task().cancelling()`` is set and the cancellation is re-raised so
    cooperative cancellation is honored (the same idiom as
    :mod:`easycat.runtime.scope` and :mod:`easycat.config._telephony_wiring`).
    """
    try:
        if timeout_s is not None:
            await asyncio.wait_for(asyncio.ensure_future(awaitable), timeout=timeout_s)
        else:
            await awaitable
    except asyncio.CancelledError:
        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling():
            raise
    except Exception:  # pragma: no cover - defensive teardown
        pass
