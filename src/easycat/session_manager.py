"""Session manager utilities for multi-connection servers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Generic, TypeVar

from easycat.session._session import Session

logger = logging.getLogger(__name__)

TKey = TypeVar("TKey")


class SessionManager(Generic[TKey]):
    """Track and lifecycle-manage many EasyCat sessions in one process.

    Concurrency contract:

    - This manager relies on :meth:`Session.stop` being idempotent. A session
      may be stopped by :meth:`remove`, :meth:`stop_all`, the ``finally`` block
      of :meth:`connection`, or an external caller; any combination of these
      must be safe. ``Session.stop`` satisfies this with an internal teardown
      guard (the ``self._closed or self._stopping`` early-return at the top of
      ``stop``) that makes repeated calls a no-op.
    - :meth:`connection` and :meth:`stop_all` (or :meth:`remove`) must **not**
      be used concurrently on overlapping keys. ``stop_all``/``remove`` tear
      the session down without signalling an in-flight ``connection`` body, so
      application code still inside the ``yield`` would be operating on an
      already-stopped session with no notification. If you need to force-stop
      sessions that may be in active ``connection`` blocks, coordinate
      cancellation at the call site (e.g. cancel the tasks running those
      blocks) before invoking ``stop_all``.
    - A key remains registered until its stop completes successfully. If a
      caller awaiting :meth:`remove` or :meth:`stop_all` is cancelled, the
      retained entry can be retried with ``force=True`` after the original stop
      coroutine has unwound.
    """

    def __init__(self) -> None:
        self._sessions: dict[TKey, Session] = {}
        self._stop_tasks: dict[TKey, tuple[Session, asyncio.Task[None], bool]] = {}
        self._force_requested: set[TKey] = set()
        self._lock = asyncio.Lock()

    def get(self, key: TKey) -> Session | None:
        return self._sessions.get(key)

    def active_keys(self) -> tuple[TKey, ...]:
        """Return a snapshot of session keys that still require teardown.

        A key remains present until its owned stop task completes successfully.
        Server lifecycle owners use this after :meth:`stop_all` to distinguish
        a genuinely clean sweep from one whose per-session failures were
        intentionally gathered so every session received a stop attempt.
        """
        return tuple(self._sessions)

    async def add(self, key: TKey, session: Session) -> Session:
        async with self._lock:
            if key in self._sessions:
                raise ValueError(f"Session key already exists: {key}")
            self._force_requested.discard(key)
            self._sessions[key] = session
        try:
            await session.start()
        except BaseException:
            # Session.start() owns partial-start teardown, including on
            # cancellation. Once it has unwound, release the manager's key
            # reservation so a replacement can reuse it. remove()/stop_all()
            # may already have released the slot while start() was in flight;
            # never erase a replacement that subsequently claimed the key.
            async with self._lock:
                if self._sessions.get(key) is session:
                    self._sessions.pop(key)
            raise
        return session

    async def remove(self, key: TKey, *, force: bool = False) -> None:
        """Stop one session, dropping its key only after successful teardown.

        The manager owns one stop task per key. Cancelling a caller waiting on
        graceful removal therefore does not lose ownership of the in-flight
        stop, and a later forced removal can cancel that exact task before
        entering ``stop(force=True)``.
        """
        while True:
            operation = await self._prepare_stop(key, force=force)
            if operation is None:
                return
            task, force_requested, operation_force = operation
            if force_requested and not operation_force:
                task.cancel()

            try:
                completed = await _await_owned_stop(task)
            except Exception:
                if force_requested and not operation_force:
                    # A failing graceful cancellation must not suppress the
                    # requested force attempt.
                    continue
                raise
            if not completed:
                # A concurrent force escalation cancelled the owned graceful
                # task. Loop so every waiter joins the replacement force task.
                continue

            if force_requested and not operation_force:
                # A cancellation-resistant graceful stop may complete normally
                # after cancellation. Its completion callback either removed
                # the session or retained it after an error; re-check both
                # pieces of keyed state before deciding whether force is needed.
                continue
            return

    async def _prepare_stop(
        self,
        key: TKey,
        *,
        force: bool,
    ) -> tuple[asyncio.Task[None], bool, bool] | None:
        """Return the current keyed stop operation, creating it when needed."""
        async with self._lock:
            session = self._sessions.get(key)
            if session is None:
                self._force_requested.discard(key)
                return None
            if force:
                self._force_requested.add(key)
            force_requested = key in self._force_requested
            operation = self._stop_tasks.get(key)
            if operation is not None and operation[1].done():
                self._finish_stop(key, operation[0], operation[1])
                session = self._sessions.get(key)
                if session is None:
                    return None
                operation = None
            if operation is None or operation[0] is not session:
                stop = session.stop(force=True) if force_requested else session.stop()
                task = asyncio.create_task(stop)
                operation = (session, task, force_requested)
                self._stop_tasks[key] = operation
                task.add_done_callback(partial(self._finish_stop, key, session))
            return operation[1], force_requested, operation[2]

    async def stop_all(self, *, force: bool = False) -> None:
        """Stop all sessions, retaining entries whose teardown did not finish."""
        async with self._lock:
            keys = list(self._sessions)
        results = await asyncio.gather(
            *(self.remove(key, force=force) for key in keys),
            return_exceptions=True,
        )
        # _finish_stop() consumes and logs each owned task's exception. Avoid
        # emitting the same failure again from this aggregate wait.
        _ = results

    def _finish_stop(
        self,
        key: TKey,
        session: Session,
        task: asyncio.Task[None],
    ) -> None:
        """Finalize manager bookkeeping for an owned stop task."""
        operation = self._stop_tasks.get(key)
        if operation is not None and operation[0] is session and operation[1] is task:
            self._stop_tasks.pop(key, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            if self._sessions.get(key) is session:
                self._sessions.pop(key, None)
                self._force_requested.discard(key)
        else:
            logger.error("Failed to stop session %s: %s", key, error)

    @asynccontextmanager
    async def connection(
        self,
        key: TKey,
        session: Session,
        *,
        runtime_feedback: bool = False,
    ) -> AsyncIterator[Session]:
        """Manage a session's lifetime within an ``async with`` block.

        The ``finally`` clause always calls :meth:`remove`, so ``session.stop``
        may run even if it was already stopped elsewhere (e.g. by
        :meth:`stop_all`); this is safe only because ``Session.stop`` is
        idempotent. Set ``runtime_feedback=True`` to attach the same console
        feedback used by the built-in multi-client server helpers before the
        session starts. Do not run :meth:`stop_all`/:meth:`remove` on this key
        concurrently with the ``yield`` body (see class docstring).
        """
        if runtime_feedback:
            from easycat.helpers import attach_runtime_feedback

            attach_runtime_feedback(session)

        await self.add(key, session)
        try:
            yield session
        finally:
            await self.remove(key)


async def _await_owned_stop(task: asyncio.Task[None]) -> bool:
    """Shield a manager-owned stop and distinguish child from caller cancellation."""
    current = asyncio.current_task()
    # Preserve a cancellation already pending at helper entry. A previously
    # caught request keeps cancelling() non-zero but does not raise here.
    if current is not None and current.cancelling():
        await asyncio.sleep(0)
    cancellation_requests = current.cancelling() if current is not None else 0
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        if current is not None and current.cancelling() > cancellation_requests:
            raise
        if task.cancelled():
            return False
        return False
    return True
