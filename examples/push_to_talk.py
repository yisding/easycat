"""Push-to-talk demo.

In ``TurnMode.PUSH_TO_TALK`` the ``TurnManager`` does not consult VAD
to start or end turns: the application calls ``session.start_turn()`` and
``session.end_turn()`` itself.  Useful for hardware push-to-talk buttons,
walkie-talkie UX, or mocked turns in tests.

This example reads a single keypress from stdin: hold ``Enter`` to mark
the start of a turn, then ``Enter`` again to mark the end.  Real
deployments would wire this to a GPIO pin, a UI button, or a hotkey.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv run python examples/push_to_talk.py
"""

from __future__ import annotations

import asyncio

from easycat import (
    EasyCatConfig,
    LocalTransportConfig,
    TurnManagerConfig,
    TurnMode,
    attach_runtime_feedback,
    create_session,
    require_env,
)


async def _stdin_loop(session) -> None:  # type: ignore[no-untyped-def]
    """Toggle a turn each time the user presses Enter."""
    print("\nPress Enter to START speaking, Enter again to END the turn.")
    print("Press Ctrl+C to quit.\n")

    loop = asyncio.get_running_loop()
    speaking = False
    while True:
        await loop.run_in_executor(None, input)
        if not speaking:
            await session.start_turn()
            print("  [turn started — speak now]")
        else:
            await session.end_turn()
            print("  [turn ended — agent is replying]")
        speaking = not speaking


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")
    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        turn_taking=TurnManagerConfig(mode=TurnMode.PUSH_TO_TALK),
        agent=agent,
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    try:
        await _stdin_loop(session)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await session.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
