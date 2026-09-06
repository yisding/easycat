"""Push-to-talk control helpers."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, Self, TextIO


class PushToTalkSession(Protocol):
    """Subset of :class:`easycat.Session` used by push-to-talk controls."""

    async def start_turn(self) -> None: ...

    async def end_turn(self) -> None: ...


class ManagedPushToTalkSession(PushToTalkSession, Protocol):
    """Push-to-talk session that also owns the async context lifecycle."""

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None: ...


# Cap the constructor-time adoption so a stream that is being written to
# continuously cannot spin the drain instead of arming the reader.
_MAX_ADOPTED_CHARS = 1 << 20

logger = logging.getLogger(__name__)


class _LineReader(Protocol):
    async def read(self) -> bool: ...

    def close(self) -> None: ...


_READ_SIZE = 4096


def _adopt_buffered_text(stream: TextIO, fd: int) -> bytes:
    """Take input an earlier buffered read already pulled off *fd*.

    A ``TextIOWrapper`` that has been read before (an application calling
    ``input()`` or ``readline()`` ahead of push-to-talk) can hold decoded
    characters that ``os.read`` will never see: those bytes already left the
    kernel and sit in the wrapper.  Claim them once, at construction, so the
    reader starts with the complete picture and owns the fd from there.

    A terminal is skipped on purpose.  Canonical-mode reads never cross a line
    boundary, so a TTY wrapper cannot be holding a pending line, and flipping
    ``O_NONBLOCK`` on a descriptor shared with the parent shell is a hazard
    worth not taking for a case that cannot arise.

    Best-effort throughout: any failure leaves the reader to start from an
    empty buffer, which is exactly the pre-existing behaviour.
    """
    try:
        if os.isatty(fd):
            return b""
        import fcntl

        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    except (OSError, ImportError, ValueError):
        return b""

    pending = ""
    try:
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        while len(pending) < _MAX_ADOPTED_CHARS:
            chunk = stream.read(_READ_SIZE)
            if not chunk:
                break
            pending += chunk
    except Exception:
        logger.debug("Could not adopt buffered stdin text", exc_info=True)
    finally:
        try:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags)
        except OSError:  # pragma: no cover - the fd was valid moments ago
            logger.debug("Could not restore stdin blocking mode", exc_info=True)

    if not pending:
        return b""
    return pending.encode(stream.encoding or "utf-8", "replace")


class _SelectorLineReader:
    """Read selectable stdin without blocking the event-loop thread.

    Reads raw bytes with :func:`os.read` and splits lines itself rather than
    calling ``stream.readline()``.  A buffered ``TextIOWrapper`` slurps every
    readable byte into its own userspace buffer, so type-ahead (two quick
    Enters) left the second line invisible to ``add_reader``: the fd reported
    no data, the callback never fired again, and every later toggle lagged a
    keypress (gh 1006).  Owning the buffer keeps kernel readiness and pending
    bytes in sync.

    Anything the stream had buffered before construction is adopted first, so
    switching to fd reads cannot strand input an earlier read had already
    pulled out of the kernel.
    """

    _READ_SIZE = _READ_SIZE

    def __init__(
        self,
        stream: TextIO,
        loop: asyncio.AbstractEventLoop,
        fd: int,
    ) -> None:
        self._stream = stream
        self._loop = loop
        self._fd = fd
        self._pending: deque[bool | Exception] = deque()
        self._ready = asyncio.Event()
        self._registered = False
        self._buffer = _adopt_buffered_text(stream, fd)
        self._publish_complete_lines()
        loop.add_reader(fd, self._on_readable)
        self._registered = True

    def _publish_complete_lines(self) -> None:
        while b"\n" in self._buffer:
            _line, _, self._buffer = self._buffer.partition(b"\n")
            self._publish(True)

    def _on_readable(self) -> None:
        try:
            data = os.read(self._fd, self._READ_SIZE)
        except (BlockingIOError, InterruptedError):
            # Spurious readability on a non-blocking fd, or a signal raced the
            # read; the reader stays armed for the next callback.
            return
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            self.close()
            self._publish(exc)
            return
        if not data:
            # EOF. A trailing unterminated line is still a line, matching
            # ``readline()``'s behaviour, and is reported before the close.
            self.close()
            if self._buffer:
                self._buffer = b""
                self._publish(True)
            self._publish(False)
            return
        self._buffer += data
        self._publish_complete_lines()

    def _publish(self, result: bool | Exception) -> None:
        self._pending.append(result)
        self._ready.set()

    async def read(self) -> bool:
        while not self._pending:
            self._ready.clear()
            await self._ready.wait()
        result = self._pending.popleft()
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        if self._registered:
            self._loop.remove_reader(self._fd)
            self._registered = False


class _ThreadLineReader:
    """Read non-selectable stdin on a daemon thread, one line at a time."""

    def __init__(self, stream: TextIO, loop: asyncio.AbstractEventLoop) -> None:
        self._stream = stream
        self._loop = loop

    async def read(self) -> bool:
        future: asyncio.Future[bool] = self._loop.create_future()

        def _complete(result: bool) -> None:
            if not future.done():
                future.set_result(result)

        def _fail(error: Exception) -> None:
            if not future.done():
                future.set_exception(error)

        def _read_once() -> None:
            try:
                result = bool(self._stream.readline())
            except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
                try:
                    self._loop.call_soon_threadsafe(_fail, exc)
                except RuntimeError:
                    pass
            else:
                try:
                    self._loop.call_soon_threadsafe(_complete, result)
                except RuntimeError:
                    pass

        threading.Thread(
            target=_read_once,
            name="easycat-stdin-reader",
            daemon=True,
        ).start()
        return await future

    def close(self) -> None:
        pass


def _stream_fileno(stream: TextIO) -> int:
    try:
        return stream.fileno()
    except (OSError, ValueError):
        return -1


def _create_line_reader(
    stream: TextIO,
    loop: asyncio.AbstractEventLoop,
) -> _LineReader:
    fd = _stream_fileno(stream)
    if fd >= 0 and sys.platform != "win32":
        try:
            return _SelectorLineReader(stream, loop, fd)
        except (NotImplementedError, RuntimeError, OSError, ValueError):
            pass
    return _ThreadLineReader(stream, loop)


@dataclass(slots=True)
class _PushToTalkController:
    session: PushToTalkSession
    print_fn: Callable[[str], None]
    speaking: bool = False

    async def toggle(self) -> None:
        if self.speaking:
            await self.session.end_turn()
            self.print_fn("  [turn ended - agent is replying]")
        else:
            await self.session.start_turn()
            self.print_fn("  [turn started - speak now]")
        self.speaking = not self.speaking


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

    reader = _create_line_reader(input_stream, asyncio.get_running_loop())
    controller = _PushToTalkController(session, print_fn)
    try:
        while await reader.read():
            await controller.toggle()
        print_fn("  [stdin closed - exiting]")
    finally:
        reader.close()


def run_stdin_push_to_talk_session(
    session: ManagedPushToTalkSession,
    *,
    input_stream: TextIO | None = None,
    print_fn: Callable[[str], None] = print,
    feedback: Literal["auto", "on", "off"] = "auto",
) -> None:
    """Run a prebuilt session with stdin push-to-talk controls.

    This is the synchronous companion to :func:`run_stdin_push_to_talk`.
    It mirrors :func:`easycat.helpers.run_session`: it applies the shared
    console-feedback policy, enters the public ``async with session:``
    lifecycle, and exits cleanly on Ctrl-C.
    """
    from easycat.helpers import (
        _enable_console_logging_from_env,
        _feedback_enabled,
        attach_runtime_feedback,
    )

    _enable_console_logging_from_env()
    if _feedback_enabled(feedback) and callable(getattr(session, "subscribe_event", None)):
        attach_runtime_feedback(session)  # type: ignore[arg-type]

    async def _run() -> None:
        async with session:
            try:
                await run_stdin_push_to_talk(
                    session,
                    input_stream=input_stream,
                    print_fn=print_fn,
                )
            except asyncio.CancelledError:
                pass

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


__all__ = [
    "ManagedPushToTalkSession",
    "PushToTalkSession",
    "run_stdin_push_to_talk",
    "run_stdin_push_to_talk_session",
]
