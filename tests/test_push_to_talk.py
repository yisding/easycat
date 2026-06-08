from __future__ import annotations

import asyncio

from easycat.push_to_talk import run_stdin_push_to_talk


class _FakeSession:
    def __init__(self) -> None:
        self.actions: list[str] = []

    async def start_turn(self) -> None:
        self.actions.append("start")

    async def end_turn(self) -> None:
        self.actions.append("end")


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
