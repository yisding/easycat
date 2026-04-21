"""Function-calling tools demo — PydanticAI.

Two tools — ``get_weather`` and ``get_time`` — registered via
``@agent.tool_plain`` because they need no per-run dependency object.
For tools that drive the call (end, transfer, DTMF) see
``session_actions_pydantic.py``.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv add easycat[pydantic-ai]
  uv run python examples/function_tools_pydantic.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from easycat import (
    EasyCatConfig,
    LocalTransportConfig,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    try:
        from pydantic_ai import Agent  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit(
            "PydanticAI is required. Install with: uv add easycat[pydantic-ai]"
        ) from exc

    voice_agent = Agent(
        "openai:gpt-5.2",
        system_prompt=(
            "You are a helpful voice assistant. "
            "Use get_weather for weather questions and get_time for time questions. "
            "Speak conversationally — you are talking, not writing."
        ),
    )

    @voice_agent.tool_plain
    def get_weather(city: str) -> str:
        """Return the current weather for a city."""
        return f"In {city} it is 18°C and partly cloudy."

    @voice_agent.tool_plain
    def get_time(timezone_name: str = "UTC") -> str:
        """Return the current wall-clock time in the named IANA timezone."""
        try:
            tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            return f"Unknown timezone: {timezone_name}."
        now = datetime.now(tz).strftime("%H:%M")
        return f"It is {now} {timezone_name}."

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        agent=voice_agent,
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
