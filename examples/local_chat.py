"""Local microphone-to-speaker voice bot demo (OpenAI Agents SDK).

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra local --extra openai
  uv sync --extra openai-agents
  uv run python examples/local_chat.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from easycat import EasyCatConfig, EchoCancellationConfig, LocalTransportConfig, create_session

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    build_openai_agents_adapter,
    default_event_logging,
    require_env,
    wait_for_shutdown_signal,
)
from runtime_feedback import attach_runtime_feedback  # noqa: E402


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")
    adapter = build_openai_agents_adapter(instructions="You are a helpful voice assistant.")

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        echo_cancellation=EchoCancellationConfig(enabled=True),
        agent=adapter,
        wrap_agent=False,
        event_logging=default_event_logging(),
    )
    session = create_session(config)

    attach_runtime_feedback(session)

    await session.start()

    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
