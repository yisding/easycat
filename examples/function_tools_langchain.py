"""Function-calling tools — LangChain (``@tool`` + ``AgentExecutor``).

Two tools (``get_weather`` and ``get_time``) wired through a
``tool_calling`` LangChain ``AgentExecutor``. The executor is itself a
``Runnable`` so ``auto_adapt_agent()`` routes it through
:class:`LangChainBridge`. For tools that drive the call (end, transfer,
DTMF) see ``session_actions_langchain.py``.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart
       uv add easycat[langchain] "langchain<1" langchain-openai
Run:   uv run python examples/function_tools_langchain.py

LangChain 1.x removed ``create_tool_calling_agent`` (the recommended
replacement, ``langchain.agents.create_agent``, returns a LangGraph
``CompiledStateGraph`` — covered by ``function_tools_langgraph.py``).
This example demonstrates the LangChain-bridge path so the version pin
keeps it runnable against the still-supported 0.3.x line.
"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from langchain.agents import AgentExecutor, create_tool_calling_agent
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI
except ImportError as exc:
    raise SystemExit(
        "LangChain (<1.0) is required. Install with: "
        'uv add easycat[langchain] "langchain<1" langchain-openai'
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


prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You are a helpful voice assistant. "
            "Use get_weather for weather questions and get_time for time. "
            "Speak conversationally — you are talking, not writing.",
        ),
        ("placeholder", "{history}"),
        ("user", "{input}"),
        ("placeholder", "{agent_scratchpad}"),
    ]
)
tools = [get_weather, get_time]
executor = AgentExecutor(
    agent=create_tool_calling_agent(ChatOpenAI(model="gpt-5.5"), tools, prompt),
    tools=tools,
)

run(EasyConfig.mic(agent=executor))
