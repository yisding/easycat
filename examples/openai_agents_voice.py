"""Local voice bot demo using a single OpenAI Agents SDK agent.

Mirror of ``examples/pydantic_ai_voice.py`` for the OpenAI Agents SDK.
For function tools see ``examples/function_tools_openai.py``.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv run python examples/openai_agents_voice.py
"""

from __future__ import annotations

import asyncio

from easycat import (
    EasyCatConfig,
    EchoCancellationConfig,
    LocalTransportConfig,
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
        echo_cancellation=EchoCancellationConfig(enabled=True),
        agent=agent,
    )
    session = create_session(config)

    attach_runtime_feedback(session)

    await session.start()

    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
