"""Function-calling tools — OpenAI Agents SDK (``@function_tool``).

Two tools (``get_weather`` and ``get_time``) with typed arguments and
docstrings the model reads as descriptions. For tools that drive the
call (end, transfer, DTMF) see ``session_actions_openai.py``.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart --group dev
       uv run easycat doctor
       uv run easycat doctor --env-file .env  # if keys live in .env
Run:   uv run python examples/function_tools_openai.py
"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from agents import Agent, function_tool  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. For an app, run: "
        "uv add 'easycat[quickstart]'. In this repo, run: uv sync --extra quickstart --group dev"
    ) from exc

from easycat import EasyConfig, run


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


run(
    EasyConfig.mic(
        agent=Agent(
            name="assistant",
            instructions=(
                "You are a helpful voice assistant. "
                "Use get_weather for weather questions and get_time for time. "
                "Speak conversationally — you are talking, not writing."
            ),
            tools=[get_weather, get_time],
        )
    )
)
