"""Function-calling tools — LangGraph (``create_react_agent``).

Two tools (``get_weather`` and ``get_time``) wired through
``langgraph.prebuilt.create_react_agent`` with an ``InMemorySaver``
checkpointer. ``auto_adapt_agent()`` routes the compiled graph through
:class:`LangGraphBridge`, which surfaces each node transition as a
``workflow_node`` cursor and each ``checkpoint_id`` as a state
snapshot. For tools that drive the call (end, transfer, DTMF) see
``session_actions_langgraph.py``.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart
       uv add easycat[langgraph] langchain-openai
Run:   uv run python examples/function_tools_langgraph.py
"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.prebuilt import create_react_agent
except ImportError as exc:
    raise SystemExit(
        "LangGraph is required. Install with: uv add easycat[langgraph] langchain-openai"
    ) from exc

from easycat import EasyConfig, run


@tool
def get_weather(city: str) -> str:
    """Return the current weather for a city."""
    return f"In {city} it is 18°C and partly cloudy."


@tool
def get_time(timezone_name: str = "UTC") -> str:
    """Return the current wall-clock time in the named IANA timezone."""
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return f"Unknown timezone: {timezone_name}."
    return f"It is {datetime.now(tz).strftime('%H:%M')} {timezone_name}."


graph = create_react_agent(
    ChatOpenAI(model="gpt-5.5"),
    tools=[get_weather, get_time],
    prompt=(
        "You are a helpful voice assistant. "
        "Use get_weather for weather questions and get_time for time. "
        "Speak conversationally — you are talking, not writing."
    ),
    checkpointer=InMemorySaver(),
)

run(EasyConfig.mic(agent=graph))
