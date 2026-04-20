"""Function-calling tools demo — OpenAI Agents SDK.

Two tools — ``get_weather`` and ``get_time`` — exercise the standard
``@function_tool`` path: synchronous return values, typed arguments,
docstrings the model reads as the tool description.  No
``SessionActions`` here; for tools that drive the call (end, transfer,
DTMF) see ``session_actions_openai.py``.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv run python examples/function_tools_openai.py
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

    from agents import Agent, function_tool  # type: ignore[import-untyped]

    @function_tool
    def get_weather(city: str) -> str:
        """Return the current weather for a city.

        A real implementation would call a weather API.  This stub returns
        a fixed reading so the example runs offline.
        """
        return f"In {city} it is 18°C and partly cloudy."

    @function_tool
    def get_time(timezone_name: str = "UTC") -> str:
        """Return the current wall-clock time in the named IANA timezone."""
        try:
            tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            return f"Unknown timezone: {timezone_name}."
        now = datetime.now(tz).strftime("%H:%M")
        return f"It is {now} {timezone_name}."

    agent = Agent(
        name="assistant",
        instructions=(
            "You are a helpful voice assistant. "
            "Use get_weather for weather questions and get_time for time questions. "
            "Speak conversationally — you are talking, not writing."
        ),
        tools=[get_weather, get_time],
    )

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        agent=agent,
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
