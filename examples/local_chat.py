"""Local microphone-to-speaker voice bot demo (OpenAI Agents SDK).

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra local --extra openai
  uv sync --extra openai-agents
  uv run python examples/local_chat.py
"""

from __future__ import annotations

import asyncio
import os
import signal

from easycat import (
    EasyCatConfig,
    LocalTransportConfig,
    OpenAIAgentsAdapter,
    create_session,
)


async def main() -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required.")

    try:
        from agents import Agent  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit(
            "OpenAI Agents SDK is required. Install with: uv sync --extra openai-agents"
        ) from exc

    voice_agent = Agent(
        name="VoiceAssistant",
        instructions="You are a helpful voice assistant.",
    )
    adapter = OpenAIAgentsAdapter(voice_agent)

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        agent=adapter,
        wrap_agent=False,
    )
    session = create_session(config)

    await session.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    await session.stop()


if __name__ == "__main__":
    asyncio.run(main())
