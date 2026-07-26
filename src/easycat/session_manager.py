"""Session manager utilities for multi-connection servers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
        self._lock = asyncio.Lock()

    def get(self, key: TKey) -> Session | None:
        return self._sessions.get(key)

    async def add(self, key: TKey, session: Session) -> Session:
        async with self._lock:
            if key in self._sessions:
                raise ValueError(f"Session key already exists: {key}")
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
        """Stop one session, dropping its key only after successful teardown."""
        async with self._lock:
            session = self._sessions.get(key)
        if session is None:
            return
        if force:
            await session.stop(force=True)
        else:
            await session.stop()
        async with self._lock:
            if self._sessions.get(key) is session:
                self._sessions.pop(key)

    async def stop_all(self, *, force: bool = False) -> None:
        """Stop all sessions, retaining entries whose teardown did not finish."""
        async with self._lock:
            sessions = list(self._sessions.items())
        results = await asyncio.gather(
            *(session.stop(force=True) if force else session.stop() for _, session in sessions),
            return_exceptions=True,
        )
        async with self._lock:
            for (key, session), result in zip(sessions, results):
                if result is None and self._sessions.get(key) is session:
                    self._sessions.pop(key)
        for (key, _session), result in zip(sessions, results):
            if isinstance(result, Exception):
                logger.error("Failed to stop session %s: %s", key, result)

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
