"""Signal helpers shared by long-running EasyCat entry points."""

from __future__ import annotations

import asyncio
import signal


def install_shutdown_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    stop_event: asyncio.Event,
) -> bool:
    """Wire SIGINT/SIGTERM to set ``stop_event`` when the loop supports it."""
    installed = False
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
            installed = True
        except (NotImplementedError, RuntimeError, ValueError):
            # NotImplementedError: ProactorEventLoop on Windows.
            # RuntimeError/ValueError: handler set off the main thread.
            pass
    return installed


def create_shutdown_event() -> asyncio.Event:
    """Return an event that is set by SIGINT/SIGTERM when supported."""
    stop_event = asyncio.Event()
    install_shutdown_signal_handlers(asyncio.get_running_loop(), stop_event)
    return stop_event
