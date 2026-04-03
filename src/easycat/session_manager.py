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
    """Track and lifecycle-manage many EasyCat sessions in one process."""

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
        await self.add(key, session)
        try:
            yield session
        finally:
            await self.remove(key)
