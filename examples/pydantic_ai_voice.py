"""Local voice bot demo using PydanticAI.

Setup:
  export OPENAI_API_KEY="..."
  uv add easycat[local]
  uv add easycat[openai]
  uv add easycat[pydantic-ai]
  uv run python examples/pydantic_ai_voice.py
"""

from __future__ import annotations

import asyncio
import os
import signal

from easycat import EasyCatConfig, LocalTransportConfig, PydanticAIAdapter, create_session


async def main() -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required.")

    try:
        from pydantic_ai import Agent  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit(
            "PydanticAI is required. Install with: uv add easycat[pydantic-ai]"
        ) from exc

    voice_agent = Agent(
        "openai:gpt-4o-mini",
        system_prompt="You are a helpful voice assistant.",
    )
    adapter = PydanticAIAdapter(voice_agent)

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
