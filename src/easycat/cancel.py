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
    GIL-free Python 3.14+).  The event loop bound to this token is captured
    at construction time (or on first ``wait()``), and ``cancel()`` uses
    ``loop.call_soon_threadsafe`` against that captured reference to set the
    ``asyncio.Event`` from non-loop threads.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._lock = threading.Lock()
        self._cancelled_at: float | None = None
        # Capture the running loop at construction time so cancel() can hop
        # back to it from non-loop threads without probing private CPython
        # internals. If no loop is running yet, it is captured lazily on the
        # first wait() call (which always runs on the loop).
        self._loop: asyncio.AbstractEventLoop | None = None
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    @property
    def is_cancelled(self) -> bool:
        # Backed by the cancellation timestamp (set under the lock) rather
        # than the asyncio.Event, so the flag is immediately and thread-safely
        # observable even when the Event.set() is deferred onto the loop.
        return self._cancelled_at is not None

    @property
    def cancelled_at(self) -> float | None:
        """Monotonic timestamp when ``cancel()`` was first called, or None."""
        return self._cancelled_at

    def cancel(self) -> None:
        """Signal cancellation to all stages checking this token.

        Safe to call from any thread. ``is_cancelled`` flips synchronously;
        coroutines blocked in ``wait()`` are woken on the bound event loop.
        """
        with self._lock:
            already = self._cancelled_at is not None
            if not already:
                self._cancelled_at = time.monotonic()
            loop = self._loop
        if already:
            return
        # asyncio.Event.set() is not thread-safe: it must run on the loop
        # thread to wake waiters. Schedule it via the captured loop when one
        # is running; otherwise set it directly (no waiters can exist yet).
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._event.set)
                return
            except RuntimeError:
                # Loop is closed/closing; fall through to a direct set.
                pass
        self._event.set()

    async def wait(self) -> None:
        """Wait until cancellation is signalled."""
        if self._loop is None:
            # First await binds the token to the loop it is waited on, so a
            # later cross-thread cancel() can schedule the set on that loop.
            self._loop = asyncio.get_running_loop()
        if self._cancelled_at is not None:
            # Already cancelled; the Event.set() may still be in flight on the
            # loop, so short-circuit instead of risking a missed wakeup.
            return
        await self._event.wait()
