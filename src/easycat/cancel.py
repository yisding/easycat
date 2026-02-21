"""Cooperative cancellation token for pipeline stages."""

from __future__ import annotations

import asyncio
import time


class CancelToken:
    """A token that pipeline stages check cooperatively for cancellation.

    Create one per turn. When `cancel()` is called, all stages checking
    `is_cancelled` will stop processing.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._cancelled_at: float | None = None

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def cancelled_at(self) -> float | None:
        """Monotonic timestamp when ``cancel()`` was first called, or None."""
        return self._cancelled_at

    def cancel(self) -> None:
        """Signal cancellation to all stages checking this token."""
        if self._cancelled_at is None:
            self._cancelled_at = time.monotonic()
        self._event.set()

    async def wait(self) -> None:
        """Wait until cancellation is signalled."""
        await self._event.wait()
