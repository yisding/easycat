"""Cooperative cancellation token for pipeline stages."""

from __future__ import annotations

import asyncio
import threading
import time


class CancelToken:
    """A token that pipeline stages check cooperatively for cancellation.

    Create one per turn. When `cancel()` is called, all stages checking
    `is_cancelled` will stop processing.

    Thread-safe: ``cancel()`` may be called from any thread (including
    GIL-free Python 3.14+).  Uses ``call_soon_threadsafe`` to set the
    ``asyncio.Event`` from non-loop threads.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._lock = threading.Lock()
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
        with self._lock:
            if self._cancelled_at is None:
                self._cancelled_at = time.monotonic()
        try:
            loop = self._event._loop  # type: ignore[attr-defined]
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._event.set)
                return
        except (AttributeError, RuntimeError):
            pass
        self._event.set()

    async def wait(self) -> None:
        """Wait until cancellation is signalled."""
        await self._event.wait()
