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
      must be safe. ``Session.stop`` satisfies this because it funnels through
      ``Session.destroy``, which guards repeated teardown.
    - :meth:`connection` and :meth:`stop_all` (or :meth:`remove`) must **not**
      be used concurrently on overlapping keys. ``stop_all``/``remove`` tear
      the session down without signalling an in-flight ``connection`` body, so
      application code still inside the ``yield`` would be operating on an
      already-stopped session with no notification. If you need to force-stop
      sessions that may be in active ``connection`` blocks, coordinate
      cancellation at the call site (e.g. cancel the tasks running those
      blocks) before invoking ``stop_all``.
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
        except Exception:
            async with self._lock:
                self._sessions.pop(key, None)
            raise
        return session

    async def remove(self, key: TKey) -> None:
        async with self._lock:
            session = self._sessions.pop(key, None)
        if session is None:
            return
        await session.stop()

    async def stop_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.items())
            self._sessions.clear()
        results = await asyncio.gather(
            *(session.stop() for _, session in sessions), return_exceptions=True
        )
        for (key, _), result in zip(sessions, results):
            if isinstance(result, Exception):
                logger.error("Failed to stop session %s: %s", key, result)

    @asynccontextmanager
    async def connection(self, key: TKey, session: Session) -> AsyncIterator[Session]:
        """Manage a session's lifetime within an ``async with`` block.

        The ``finally`` clause always calls :meth:`remove`, so ``session.stop``
        may run even if it was already stopped elsewhere (e.g. by
        :meth:`stop_all`); this is safe only because ``Session.stop`` is
        idempotent. Do not run :meth:`stop_all`/:meth:`remove` on this key
        concurrently with the ``yield`` body (see class docstring).
        """
        await self.add(key, session)
        try:
            yield session
        finally:
            await self.remove(key)
