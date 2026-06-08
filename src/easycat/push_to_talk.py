"""Push-to-talk control helpers."""

from __future__ import annotations

import asyncio
import sys
import threading
from collections import deque
from collections.abc import Callable
from typing import Protocol, TextIO


class PushToTalkSession(Protocol):
    """Subset of :class:`easycat.Session` used by push-to-talk controls."""

    async def start_turn(self) -> None: ...

    async def end_turn(self) -> None: ...


async def run_stdin_push_to_talk(
    session: PushToTalkSession,
    *,
    input_stream: TextIO | None = None,
    print_fn: Callable[[str], None] = print,
) -> None:
    """Toggle ``session.start_turn()`` / ``end_turn()`` on each Enter press.

    Unix TTYs use ``loop.add_reader`` so Ctrl-C can stop cleanly without a
    blocked input thread. Windows and non-selectable stdin fall back to a
    daemon reader thread; EOF exits the loop instead of spinning.
    """
    input_stream = input_stream or sys.stdin
    print_fn("\nPress Enter to START speaking, Enter again to END the turn.")
    print_fn("Press Ctrl+C to quit.\n")

    loop = asyncio.get_running_loop()
    pending: deque[bool] = deque()
    ready = asyncio.Event()

    def _enqueue(got_line: bool) -> None:
        pending.append(got_line)
        ready.set()

    try:
        fd = input_stream.fileno()
    except (OSError, ValueError):
        fd = -1

    use_reader = False
    if fd >= 0 and sys.platform != "win32":

        def _on_stdin() -> None:
            line = input_stream.readline()
            if not line:
                loop.remove_reader(fd)
            _enqueue(bool(line))

        try:
            loop.add_reader(fd, _on_stdin)
            use_reader = True
        except (NotImplementedError, RuntimeError, OSError, ValueError):
            pass

    async def _read_from_thread() -> bool:
        future = loop.create_future()

        def _read_once() -> None:
            line = input_stream.readline()

            def _complete() -> None:
                if not future.done():
                    future.set_result(bool(line))

            try:
                loop.call_soon_threadsafe(_complete)
            except RuntimeError:
                pass

        threading.Thread(target=_read_once, daemon=True).start()
        return await future

    async def _next_line() -> bool:
        if not use_reader:
            return await _read_from_thread()
        while not pending:
            ready.clear()
            await ready.wait()
        return pending.popleft()

    speaking = False
    try:
        while True:
            got = await _next_line()
            if not got:
                print_fn("  [stdin closed - exiting]")
                return
            if not speaking:
                await session.start_turn()
                print_fn("  [turn started - speak now]")
            else:
                await session.end_turn()
                print_fn("  [turn ended - agent is replying]")
            speaking = not speaking
    finally:
        if use_reader:
            loop.remove_reader(fd)
