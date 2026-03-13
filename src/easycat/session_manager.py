"""Session manager utilities for multi-connection servers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Generic, TypeVar

from easycat.session import Session

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
        await asyncio.gather(*(session.stop() for _, session in sessions), return_exceptions=False)

    @asynccontextmanager
    async def connection(self, key: TKey, session: Session) -> AsyncIterator[Session]:
        await self.add(key, session)
        try:
            yield session
        finally:
            await self.remove(key)
