"""Signal helpers shared by long-running EasyCat entry points."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import FrameType
from typing import Any

OSSignalHandler = Callable[[int, FrameType | None], Any] | int | None


def _try_install_signal_handler(
    loop: asyncio.AbstractEventLoop,
    sig: signal.Signals,
    callback: Callable[[], None],
) -> bool:
    try:
        loop.add_signal_handler(sig, callback)
    except (NotImplementedError, RuntimeError, ValueError):
        # NotImplementedError: ProactorEventLoop on Windows.
        # RuntimeError/ValueError: handler set off the main thread.
        return False
    return True


def install_shutdown_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    stop_event: asyncio.Event,
) -> bool:
    """Wire SIGINT/SIGTERM to set ``stop_event`` when the loop supports it."""
    installed = False
    for sig in (signal.SIGINT, signal.SIGTERM):
        installed = _try_install_signal_handler(loop, sig, stop_event.set) or installed
    return installed


def _loop_handler_snapshot(
    loop: asyncio.AbstractEventLoop,
    sig: signal.Signals,
) -> tuple[Callable[..., Any], tuple[Any, ...]] | None:
    """Best-effort snapshot of an asyncio-owned handler before replacement.

    asyncio does not expose a public getter for callbacks registered through
    ``add_signal_handler``. CPython and compatible loops retain ``Handle``
    objects in ``_signal_handlers``; reading that registry lets an embedded
    EasyCat run restore a host callback instead of only restoring asyncio's
    low-level no-op OS handler. Other loops fall back to the OS-level snapshot.
    """
    registry = getattr(loop, "_signal_handlers", None)
    if not isinstance(registry, dict):
        return None
    handle = registry.get(sig)
    if handle is None or handle.cancelled():
        return None
    callback = getattr(handle, "_callback", None)
    args = getattr(handle, "_args", ())
    if not callable(callback) or not isinstance(args, tuple):
        return None
    return callback, args


def _restore_signal_handler(
    loop: asyncio.AbstractEventLoop,
    sig: signal.Signals,
    os_handler: OSSignalHandler,
    loop_handler: tuple[Callable[..., Any], tuple[Any, ...]] | None,
) -> None:
    """Best-effort restoration for one temporary signal registration."""
    try:
        loop.remove_signal_handler(sig)
    except (NotImplementedError, RuntimeError, ValueError):
        pass

    if loop_handler is not None:
        callback, args = loop_handler
        try:
            loop.add_signal_handler(sig, callback, *args)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        else:
            return
    if os_handler is not None:
        try:
            signal.signal(sig, os_handler)
        except (OSError, RuntimeError, ValueError):
            pass


@contextmanager
def scoped_shutdown_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    stop_event: asyncio.Event,
) -> Iterator[bool]:
    """Temporarily route SIGINT/SIGTERM to ``stop_event`` and restore the host.

    Use this for library helpers that run inside an event loop owned by a
    notebook, ASGI server, test harness, or another framework. Process-level
    server entry points can continue using :func:`create_shutdown_event`, whose
    handlers intentionally live for the process lifetime.
    """
    installed: list[
        tuple[
            signal.Signals,
            OSSignalHandler,
            tuple[Callable[..., Any], tuple[Any, ...]] | None,
        ]
    ] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            os_handler = signal.getsignal(sig)
        except (OSError, ValueError):
            os_handler = None
        loop_handler = _loop_handler_snapshot(loop, sig)
        if _try_install_signal_handler(loop, sig, stop_event.set):
            installed.append((sig, os_handler, loop_handler))

    try:
        yield bool(installed)
    finally:
        for sig, os_handler, loop_handler in reversed(installed):
            _restore_signal_handler(loop, sig, os_handler, loop_handler)


def create_shutdown_event() -> asyncio.Event:
    """Return an event that is set by SIGINT/SIGTERM when supported."""
    stop_event = asyncio.Event()
    install_shutdown_signal_handlers(asyncio.get_running_loop(), stop_event)
    return stop_event
