"""Smart-turn endpoint detection demo.

By default ``TurnManager`` waits ``end_of_turn_silence_ms`` (1 second)
of silence before declaring the turn over.  Smart-turn classifies the
captured audio with a small ONNX model (~8 MB Whisper-Tiny) and ends
the turn early when it is confident the user is done — typically
shaving a few hundred ms off the response time.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart --extra smart-turn
  uv run python examples/smart_turn_demo.py
"""

from __future__ import annotations

import asyncio

from easycat import (
    EasyCatConfig,
    LocalTransportConfig,
    SmartTurnConfig,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")
    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        smart_turn=SmartTurnConfig(enabled=True, threshold=0.5),
        agent=agent,
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
