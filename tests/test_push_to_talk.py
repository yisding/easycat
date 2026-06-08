from __future__ import annotations

import asyncio

import pytest

from easycat.push_to_talk import run_stdin_push_to_talk, run_stdin_push_to_talk_session


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

    async def __aenter__(self) -> _ManagedFakeSession:
        self.entered = True
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.exited = True


class _Lines:
    def __init__(self, lines: list[str]) -> None:
        self._lines = iter(lines)

    def fileno(self) -> int:
        raise ValueError("not selectable")

    def readline(self) -> str:
        return next(self._lines, "")


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


def test_run_stdin_push_to_talk_session_owns_lifecycle_and_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _ManagedFakeSession()
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
