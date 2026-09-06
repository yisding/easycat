from __future__ import annotations

import asyncio
import os
import sys
from typing import Self

import pytest

from easycat.push_to_talk import (
    _SelectorLineReader,
    run_stdin_push_to_talk,
    run_stdin_push_to_talk_session,
)


class _FakeSession:
    def __init__(self) -> None:
        self.actions: list[str] = []

    async def start_turn(self) -> None:
        self.actions.append("start")

    async def end_turn(self) -> None:
        self.actions.append("end")


class _ManagedFakeSession(_FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> Self:
        self.entered = True
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.exited = True


class _FeedbackManagedFakeSession(_ManagedFakeSession):
    def subscribe_event(self, event_type: object, callback: object) -> None:
        pass


class _Lines:
    def __init__(self, lines: list[str]) -> None:
        self._lines = iter(lines)

    def fileno(self) -> int:
        raise ValueError("not selectable")

    def readline(self) -> str:
        return next(self._lines, "")


class _ApparentlySelectableLines(_Lines):
    def fileno(self) -> int:
        return 0


class _RaisingLines:
    def __init__(self, error: RuntimeError) -> None:
        self.error = error

    def fileno(self) -> int:
        raise ValueError("not selectable")

    def readline(self) -> str:
        raise self.error


class _RaisingSelectable:
    def __init__(self, fd: int, error: RuntimeError) -> None:
        self._fd = fd
        self.error = error

    def fileno(self) -> int:
        return self._fd


async def test_run_stdin_push_to_talk_toggles_turns_from_enter_presses() -> None:
    session = _FakeSession()
    output: list[str] = []

    await asyncio.wait_for(
        run_stdin_push_to_talk(
            session,
            input_stream=_Lines(["\n", "\n", ""]),  # type: ignore[arg-type]
            print_fn=output.append,
        ),
        timeout=1,
    )

    assert session.actions == ["start", "end"]
    assert "  [turn started - speak now]" in output
    assert "  [turn ended - agent is replying]" in output
    assert "  [stdin closed - exiting]" in output


async def test_run_stdin_push_to_talk_exits_on_eof_without_turns() -> None:
    session = _FakeSession()
    output: list[str] = []

    await asyncio.wait_for(
        run_stdin_push_to_talk(
            session,
            input_stream=_Lines([""]),  # type: ignore[arg-type]
            print_fn=output.append,
        ),
        timeout=1,
    )

    assert session.actions == []
    assert output[-1] == "  [stdin closed - exiting]"


@pytest.mark.skipif(sys.platform == "win32", reason="add_reader is a Unix stdin path")
async def test_run_stdin_push_to_talk_reads_selectable_pipe() -> None:
    session = _FakeSession()
    output: list[str] = []
    read_fd, write_fd = os.pipe()

    try:
        with os.fdopen(read_fd) as stream:
            os.write(write_fd, b"\n\n")
            os.close(write_fd)
            write_fd = -1
            await asyncio.wait_for(
                run_stdin_push_to_talk(session, input_stream=stream, print_fn=output.append),
                timeout=1,
            )
    finally:
        if write_fd >= 0:
            os.close(write_fd)

    assert session.actions == ["start", "end"]
    assert output[-1] == "  [stdin closed - exiting]"


@pytest.mark.skipif(sys.platform == "win32", reason="add_reader is a Unix stdin path")
async def test_selectable_reader_delivers_type_ahead_without_eof() -> None:
    """Two quick Enters must both toggle while the pipe stays open (gh 1006).

    The sibling test above closes the write end immediately, so EOF
    re-triggers readability and masks a swallowed second line.  Here the pipe
    stays open: a buffered ``readline()`` would leave the second Enter stuck
    in the ``TextIOWrapper`` with the fd reporting no data.
    """
    loop = asyncio.get_running_loop()
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd) as stream:
            reader = _SelectorLineReader(stream, loop, read_fd)
            try:
                os.write(write_fd, b"\n\n")
                assert await asyncio.wait_for(reader.read(), timeout=1) is True
                assert await asyncio.wait_for(reader.read(), timeout=1) is True

                # A partial line stays pending until its newline arrives.
                os.write(write_fd, b"partial")
                await asyncio.sleep(0)
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(reader.read(), timeout=0.1)
                os.write(write_fd, b"\n")
                assert await asyncio.wait_for(reader.read(), timeout=1) is True
            finally:
                reader.close()
    finally:
        os.close(write_fd)


@pytest.mark.skipif(sys.platform == "win32", reason="add_reader is a Unix stdin path")
async def test_selectable_reader_adopts_text_already_buffered_by_the_stream() -> None:
    """Input an earlier buffered read pulled off the fd must not be stranded.

    An application that calls ``input()`` / ``readline()`` before starting
    push-to-talk leaves the following lines in the ``TextIOWrapper``'s decoded
    buffer — already out of the kernel, so ``os.read`` can never see them.
    """
    loop = asyncio.get_running_loop()
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd) as stream:
            os.write(write_fd, b"consumed\npending-one\npending-two\n")
            # The prior read pulls *everything* into the wrapper's buffer.
            assert stream.readline() == "consumed\n"

            reader = _SelectorLineReader(stream, loop, read_fd)
            try:
                assert await asyncio.wait_for(reader.read(), timeout=1) is True
                assert await asyncio.wait_for(reader.read(), timeout=1) is True
                # Nothing was fabricated beyond the two buffered lines.
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(reader.read(), timeout=0.1)
                # ...and the fd path still works afterwards.
                os.write(write_fd, b"live\n")
                assert await asyncio.wait_for(reader.read(), timeout=1) is True
            finally:
                reader.close()
    finally:
        os.close(write_fd)


@pytest.mark.skipif(sys.platform == "win32", reason="add_reader is a Unix stdin path")
async def test_selectable_reader_adoption_holds_a_partial_buffered_line() -> None:
    """A buffered partial line waits for its newline instead of firing early."""
    loop = asyncio.get_running_loop()
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd) as stream:
            os.write(write_fd, b"consumed\npart")
            assert stream.readline() == "consumed\n"

            reader = _SelectorLineReader(stream, loop, read_fd)
            try:
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(reader.read(), timeout=0.1)
                os.write(write_fd, b"ial\n")
                assert await asyncio.wait_for(reader.read(), timeout=1) is True
            finally:
                reader.close()
    finally:
        os.close(write_fd)


@pytest.mark.skipif(sys.platform == "win32", reason="add_reader is a Unix stdin path")
async def test_selectable_reader_leaves_the_fd_blocking_after_adoption() -> None:
    """The O_NONBLOCK flip used to adopt buffered text must be reverted."""
    import fcntl

    loop = asyncio.get_running_loop()
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd) as stream:
            reader = _SelectorLineReader(stream, loop, read_fd)
            try:
                flags = fcntl.fcntl(read_fd, fcntl.F_GETFL)
                assert not flags & os.O_NONBLOCK
            finally:
                reader.close()
    finally:
        os.close(write_fd)


@pytest.mark.skipif(sys.platform == "win32", reason="add_reader is a Unix stdin path")
async def test_selectable_reader_skips_adoption_for_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TTY never buffers a pending line, and must not be flipped to O_NONBLOCK.

    Canonical-mode reads cannot cross a line boundary, so the adoption has
    nothing to find — and the descriptor is shared with the parent shell.
    """
    import fcntl

    loop = asyncio.get_running_loop()
    read_fd, write_fd = os.pipe()
    touched: list[int] = []
    real_fcntl = fcntl.fcntl

    def _tracked(fd: int, op: int, *args: object):
        if op == fcntl.F_SETFL:
            touched.append(fd)
        return real_fcntl(fd, op, *args)

    monkeypatch.setattr("fcntl.fcntl", _tracked)
    monkeypatch.setattr(os, "isatty", lambda fd: fd == read_fd)
    try:
        with os.fdopen(read_fd) as stream:
            reader = _SelectorLineReader(stream, loop, read_fd)
            reader.close()
    finally:
        os.close(write_fd)

    assert touched == []


@pytest.mark.skipif(sys.platform == "win32", reason="add_reader is a Unix stdin path")
async def test_selectable_reader_reports_unterminated_tail_then_eof() -> None:
    loop = asyncio.get_running_loop()
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd) as stream:
        reader = _SelectorLineReader(stream, loop, read_fd)
        try:
            os.write(write_fd, b"tail")
            os.close(write_fd)
            assert await asyncio.wait_for(reader.read(), timeout=1) is True
            assert await asyncio.wait_for(reader.read(), timeout=1) is False
        finally:
            reader.close()


@pytest.mark.skipif(sys.platform == "win32", reason="add_reader is a Unix stdin path")
async def test_run_stdin_push_to_talk_toggles_on_type_ahead() -> None:
    session = _FakeSession()
    output: list[str] = []
    read_fd, write_fd = os.pipe()

    async def _drive() -> None:
        with os.fdopen(read_fd) as stream:
            await run_stdin_push_to_talk(session, input_stream=stream, print_fn=output.append)

    try:
        task = asyncio.create_task(_drive())
        os.write(write_fd, b"\n\n")
        for _ in range(200):
            if session.actions == ["start", "end"]:
                break
            await asyncio.sleep(0.005)
        assert session.actions == ["start", "end"]
    finally:
        os.close(write_fd)
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.skipif(sys.platform == "win32", reason="add_reader is a Unix stdin path")
async def test_selectable_reader_is_removed_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    loop = asyncio.get_running_loop()
    removed: list[int] = []
    original_remove_reader = loop.remove_reader
    read_fd, write_fd = os.pipe()

    def _record_remove(fd: int) -> bool:
        removed.append(fd)
        return original_remove_reader(fd)

    monkeypatch.setattr(loop, "remove_reader", _record_remove)
    try:
        with os.fdopen(read_fd) as stream:
            task = asyncio.create_task(run_stdin_push_to_talk(session, input_stream=stream))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        os.close(write_fd)

    assert removed == [read_fd]


async def test_thread_reader_propagates_read_failure() -> None:
    error = RuntimeError("stdin failed")

    with pytest.raises(RuntimeError, match="stdin failed") as exc_info:
        await asyncio.wait_for(
            run_stdin_push_to_talk(
                _FakeSession(),
                input_stream=_RaisingLines(error),  # type: ignore[arg-type]
            ),
            timeout=1,
        )

    assert exc_info.value is error


@pytest.mark.skipif(sys.platform == "win32", reason="add_reader is skipped on Windows")
async def test_add_reader_failure_uses_thread_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()

    def _unsupported(_fd: int, _callback: object) -> None:
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_reader", _unsupported)
    session = _FakeSession()
    await asyncio.wait_for(
        run_stdin_push_to_talk(
            session,
            input_stream=_ApparentlySelectableLines(["\n", ""]),  # type: ignore[arg-type]
        ),
        timeout=1,
    )

    assert session.actions == ["start"]


@pytest.mark.skipif(sys.platform == "win32", reason="add_reader is a Unix stdin path")
async def test_selector_reader_propagates_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    error = RuntimeError("stdin failed")

    def _raising_read(fd: int, size: int) -> bytes:
        raise error

    monkeypatch.setattr("easycat.push_to_talk.os.read", _raising_read)
    try:
        os.write(write_fd, b"x")
        with pytest.raises(RuntimeError, match="stdin failed") as exc_info:
            await asyncio.wait_for(
                run_stdin_push_to_talk(
                    _FakeSession(),
                    input_stream=_RaisingSelectable(read_fd, error),  # type: ignore[arg-type]
                ),
                timeout=1,
            )
        assert exc_info.value is error
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.skipif(sys.platform == "win32", reason="add_reader is a Unix stdin path")
async def test_selector_reader_stays_armed_after_transient_read_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spurious wakeup on a non-blocking fd must not end the reader."""
    loop = asyncio.get_running_loop()
    read_fd, write_fd = os.pipe()
    real_read = os.read
    calls: list[int] = []

    def _flaky_read(fd: int, size: int) -> bytes:
        calls.append(fd)
        if len(calls) == 1:
            raise BlockingIOError
        return real_read(fd, size)

    monkeypatch.setattr("easycat.push_to_talk.os.read", _flaky_read)
    try:
        with os.fdopen(read_fd) as stream:
            reader = _SelectorLineReader(stream, loop, read_fd)
            try:
                os.write(write_fd, b"\n")
                assert await asyncio.wait_for(reader.read(), timeout=1) is True
                assert len(calls) >= 2
            finally:
                reader.close()
    finally:
        os.close(write_fd)


def test_run_stdin_push_to_talk_session_owns_lifecycle_and_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FeedbackManagedFakeSession()
    output: list[str] = []
    attached: list[object] = []

    monkeypatch.setattr("easycat.helpers.attach_runtime_feedback", attached.append)

    run_stdin_push_to_talk_session(
        session,
        input_stream=_Lines(["\n", "\n", ""]),  # type: ignore[arg-type]
        print_fn=output.append,
        feedback="on",
    )

    assert session.entered is True
    assert session.exited is True
    assert session.actions == ["start", "end"]
    assert attached == [session]
    assert "  [turn started - speak now]" in output
    assert "  [turn ended - agent is replying]" in output


def test_run_stdin_push_to_talk_session_skips_feedback_for_minimal_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _ManagedFakeSession()
    output: list[str] = []
    attached: list[object] = []

    monkeypatch.setattr("easycat.helpers.attach_runtime_feedback", attached.append)

    run_stdin_push_to_talk_session(
        session,
        input_stream=_Lines(["\n", ""]),  # type: ignore[arg-type]
        print_fn=output.append,
        feedback="on",
    )

    assert session.entered is True
    assert session.exited is True
    assert session.actions == ["start"]
    assert attached == []
    assert "  [turn started - speak now]" in output
