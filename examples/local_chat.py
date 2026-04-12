"""Local microphone-to-speaker voice bot demo (OpenAI Agents SDK).

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv run python examples/local_chat.py
"""

from __future__ import annotations

import asyncio

from easycat import (
    EasyCatConfig,
    EchoCancellationConfig,
    LocalTransportConfig,
    attach_runtime_feedback,
    create_session,
    default_event_logging,
    require_env,
    wait_for_shutdown_signal,
)


async def main() -> None:
    from agents import Agent  # type: ignore[import-untyped]

    api_key = require_env("OPENAI_API_KEY")
    agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        echo_cancellation=EchoCancellationConfig(enabled=True),
        agent=agent,
        event_logging=default_event_logging(),
    )
    session = create_session(config)

    attach_runtime_feedback(session)

    await session.start()

    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
