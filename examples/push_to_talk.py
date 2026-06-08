"""Push-to-talk demo.

In ``TurnMode.PUSH_TO_TALK`` the ``TurnManager`` does not consult VAD
to start or end turns: the application calls ``session.start_turn()`` and
``session.end_turn()`` itself.  Useful for hardware push-to-talk buttons,
walkie-talkie UX, or mocked turns in tests.

This example reads Enter presses from stdin: press ``Enter`` to mark
the start of a turn, then ``Enter`` again to mark the end.  Real
deployments would wire this to a GPIO pin, a UI button, or a hotkey.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart --group dev
  uv run easycat doctor
  uv run easycat doctor --env-file .env  # if keys live in .env
  uv run easycat doctor --env-file .env --json  # for parseable checks
  uv run python examples/push_to_talk.py
  uv run --env-file .env python examples/push_to_talk.py  # if keys live in .env
"""

from __future__ import annotations

import asyncio

from easycat import (
    EasyConfig,
    LocalTransportConfig,
    TurnManagerConfig,
    TurnMode,
    attach_runtime_feedback,
    create_session,
    require_env,
)
from easycat.push_to_talk import run_stdin_push_to_talk


async def main() -> None:
    require_env("OPENAI_API_KEY")
    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")

    config = EasyConfig(
        transport=LocalTransportConfig(),
        turn_taking=TurnManagerConfig(mode=TurnMode.PUSH_TO_TALK),
        agent=agent,
    )

    async with create_session(config) as session:
        attach_runtime_feedback(session)
        try:
            await run_stdin_push_to_talk(session)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
