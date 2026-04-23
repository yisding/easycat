"""Subscribe to agent and tool-call events at the session level.

``Session.subscribe_agent_events`` attaches handlers for the agent's
streaming text and every tool call the agent makes, without having to
touch the underlying EventBus.  The returned list of registrations can be
fed back into ``session.unsubscribe_handlers`` for a single-call teardown.

Reuses the weather/time tools from ``function_tools_openai.py`` so the
tool-call events actually fire — ask the assistant about the weather or
the time in some city and watch the stream of ``[agent delta]``,
``[tool started]``, ``[tool result]``, and ``[agent final]`` lines.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv run python examples/agent_event_subscription.py
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
from easycat.events import (
    AgentDelta,
    AgentFinal,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
)


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    from agents import Agent, function_tool  # type: ignore[import-untyped]

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

    agent = Agent(
        name="assistant",
        instructions=(
            "You are a helpful voice assistant. "
            "Use get_weather for weather and get_time for time. "
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

    def on_delta(event: AgentDelta) -> None:
        print(f"[agent delta] {event.text!r}")

    def on_final(event: AgentFinal) -> None:
        print(f"[agent final] {event.text!r}")

    def on_tool_started(event: ToolCallStarted) -> None:
        print(f"[tool started] {event.tool_name} (call_id={event.call_id})")

    def on_tool_delta(event: ToolCallDelta) -> None:
        print(f"[tool delta] call_id={event.call_id} delta={event.delta!r}")

    def on_tool_result(event: ToolCallResult) -> None:
        print(f"[tool result] call_id={event.call_id} result={event.result!r}")

    registrations = session.subscribe_agent_events(
        on_delta=on_delta,
        on_final=on_final,
        on_tool_started=on_tool_started,
        on_tool_delta=on_tool_delta,
        on_tool_result=on_tool_result,
    )

    try:
        await session.start()
        await wait_for_shutdown_signal(session)
    finally:
        # Detach every handler we registered in one call.
        session.unsubscribe_handlers(registrations)


if __name__ == "__main__":
    asyncio.run(main())
