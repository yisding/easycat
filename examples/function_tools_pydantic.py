"""Function-calling tools — PydanticAI (``@agent.tool_plain``).

Two tools (``get_weather`` and ``get_time``) registered without a deps
object. For tools that drive the call (end, transfer, DTMF) see
``session_actions_pydantic.py``.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart; uv add easycat[pydantic-ai]
Run:   uv run python examples/function_tools_pydantic.py
"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from pydantic_ai import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit("PydanticAI is required. Install with: uv add easycat[pydantic-ai]") from exc

from easycat import EasyCatConfig, run

voice_agent = Agent(
    "openai:gpt-5.2",
    system_prompt=(
        "You are a helpful voice assistant. "
        "Use get_weather for weather questions and get_time for time. "
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
    return f"It is {datetime.now(tz).strftime('%H:%M')} {timezone_name}."


run(EasyCatConfig.mic(agent=voice_agent))
