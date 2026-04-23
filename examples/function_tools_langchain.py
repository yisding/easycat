"""Function-calling tools demo — LangChain.

Two tools — ``get_weather`` and ``get_time`` — wired through a
``tool_calling`` LangChain ``AgentExecutor``.  The executor is itself a
``Runnable`` so ``auto_adapt_agent()`` routes it through
:class:`LangChainBridge` and the bridge surfaces tool calls + cursor
transitions into the EasyCat journal.

For tools that drive the call (end, transfer, DTMF) see
``session_actions_langchain.py``.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv add easycat[langchain] langchain langchain-openai
  uv run python examples/function_tools_langchain.py
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
        from langchain.agents import AgentExecutor, create_tool_calling_agent
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.tools import tool
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise SystemExit(
            "LangChain is required. "
            "Install with: uv add easycat[langchain] langchain langchain-openai"
        ) from exc

    @tool
    def get_weather(city: str) -> str:
        """Return the current weather for a city.

        A real implementation would call a weather API.  This stub returns
        a fixed reading so the example runs offline.
        """
        return f"In {city} it is 18°C and partly cloudy."

    @tool
    def get_time(timezone_name: str = "UTC") -> str:
        """Return the current wall-clock time in the named IANA timezone."""
        try:
            tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            return f"Unknown timezone: {timezone_name}."
        now = datetime.now(tz).strftime("%H:%M")
        return f"It is {now} {timezone_name}."

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You are a helpful voice assistant. "
                "Use get_weather for weather questions and get_time for time questions. "
                "Speak conversationally — you are talking, not writing.",
            ),
            ("placeholder", "{history}"),
            ("user", "{input}"),
            ("placeholder", "{agent_scratchpad}"),
        ]
    )
    model = ChatOpenAI(model="gpt-4o-mini", api_key=api_key)
    tools = [get_weather, get_time]
    agent = create_tool_calling_agent(model, tools, prompt)
    executor = AgentExecutor(agent=agent, tools=tools)

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        agent=executor,  # auto_adapt_agent() routes through LangChainBridge
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
