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
from collections.abc import Awaitable, Callable, Hashable, Iterable
from functools import partial
from typing import Generic, TypeVar

KeyT = TypeVar("KeyT", bound=Hashable)


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
        if self.active_count == 0:
            return True
        deadline = asyncio.get_running_loop().time() + max(timeout_s, 0.0)
        while self.active_count > 0:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(max(poll_interval_s, 0.001), remaining))
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
        3. For any session whose graceful stop did NOT finish, CANCEL and reap
           the still-pending graceful task first. This clears the real
           ``Session._stopping`` guard before force escalation.
        4. Call ``session.stop(force=True)`` after the cancelled graceful task
           has unwound, so the force path can perform backend teardown.
        5. Untrack every drained key.

        ``drain_timeout_s <= 0`` (the ``force=True`` path) collapses the grace
        window to zero so every session is force-escalated immediately.

        ``force_timeout_s`` (default ``None`` = unbounded) is one hard deadline
        for the concurrent forced phase. Work still running at the deadline
        remains owned in the background rather than making the caller wait for
        cancellation-resistant teardown.
        """
        pairs = list(sessions_for_keys())
        if not pairs:
            return

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
            # Teardown ownership survives cancellation of the caller running
            # drain. This prevents graceful tasks from becoming detached and
            # preserves a keyed path for later force escalation.
            finish_task = asyncio.create_task(
                self._finish_drain(
                    graceful,
                    force_after=force_after,
                    force_timeout_s=force_timeout_s,
                    stop_for_key=stop_for_key,
                )
            )
            self._drain_tasks.add(finish_task)
            finish_task.add_done_callback(self._drain_tasks.discard)

        await asyncio.shield(finish_task)

    async def _finish_drain(
        self,
        graceful: dict[KeyT, tuple[object, asyncio.Task[None] | None]],
        *,
        force_after: bool,
        force_timeout_s: float | None,
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
                timeout_s=force_timeout_s,
            )
        for key in graceful:
            self.untrack(key)

    def _untrack_after_escalation(self, key: KeyT, _task: asyncio.Task[None]) -> None:
        self.untrack(key)


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
    ``current_task().cancelling()`` is set and the cancellation is re-raised so
    cooperative cancellation is honored (the same idiom as
    :mod:`easycat.runtime.scope` and :mod:`easycat.config._telephony_wiring`).
    """
    try:
        if timeout_s is not None:
            await _await_with_hard_timeout(awaitable, timeout_s=timeout_s)
        else:
            await awaitable
    except asyncio.CancelledError:
        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling():
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
