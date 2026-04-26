"""Subscribe to agent + tool-call events at the session level.

``Session.subscribe_agent_events`` attaches handlers for the agent's
streaming text and every tool call without touching the EventBus
directly. Reuses the weather/time tools so tool events actually fire.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart
Run:   uv run python examples/agent_event_subscription.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from agents import Agent, function_tool  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. Install with: uv sync --extra quickstart"
    ) from exc

from easycat import (
    EasyCatConfig,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)


@function_tool
def get_weather(city: str) -> str:
    """Return the current weather for a city."""
    return f"In {city} it is 18°C and partly cloudy."


@function_tool
def get_time(timezone_name: str = "UTC") -> str:
    """Return the current wall-clock time in the named IANA timezone."""
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return f"Unknown timezone: {timezone_name}."
    return f"It is {datetime.now(tz).strftime('%H:%M')} {timezone_name}."


async def main() -> None:
    require_env("OPENAI_API_KEY")
    session = create_session(
        EasyCatConfig.mic(
            agent=Agent(
                name="assistant",
                instructions=(
                    "You are a helpful voice assistant. "
                    "Use get_weather for weather and get_time for time."
                ),
                tools=[get_weather, get_time],
            ),
        )
    )
    attach_runtime_feedback(session)

    registrations = session.subscribe_agent_events(
        on_delta=lambda e: print(f"[agent delta] {e.text!r}"),
        on_final=lambda e: print(f"[agent final] {e.text!r}"),
        on_tool_started=lambda e: print(f"[tool started] {e.tool_name} ({e.call_id})"),
        on_tool_delta=lambda e: print(f"[tool delta] {e.call_id} {e.delta!r}"),
        on_tool_result=lambda e: print(f"[tool result] {e.call_id} {e.result!r}"),
    )

    try:
        await session.start()
        await wait_for_shutdown_signal(session)
    finally:
        session.unsubscribe_handlers(registrations)


if __name__ == "__main__":
    asyncio.run(main())
