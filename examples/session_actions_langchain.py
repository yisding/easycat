"""Session actions demo — LangChain.

Shows the simplest session action that works on every transport: ending
the session after the current reply finishes.  The ``end_call`` tool
closes over the module-level :class:`SessionActions` object — LangChain
tools receive arguments only, so there's no context/deps parameter to
thread through :class:`LangChainBridge`.

For telephony-specific actions such as transfer, DTMF, and SMS, use the
Twilio example instead: ``examples/twilio_app.py``.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv add easycat[langchain] langchain langchain-openai
  uv run python examples/session_actions_langchain.py
"""

from __future__ import annotations

import asyncio

from easycat import (
    EasyCatConfig,
    LocalTransportConfig,
    SessionActions,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)

# ── Shared action queue ──────────────────────────────────────────
# The same SessionActions instance is captured by the end_call tool's
# closure AND passed to EasyCatConfig.  Tools enqueue; Session drains.

actions = SessionActions()


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
    def end_call(reason: str = "") -> str:
        """End the call gracefully. Use when the user says goodbye."""
        actions.end_call(reason=reason)
        return "Ending the call now."

    prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                "You are a helpful voice assistant. "
                "When the user says goodbye, use the end_call tool. "
                "Be concise — you are speaking, not writing.",
            ),
            ("placeholder", "{history}"),
            ("user", "{input}"),
            ("placeholder", "{agent_scratchpad}"),
        ]
    )
    model = ChatOpenAI(model="gpt-4o-mini", api_key=api_key)
    tools = [end_call]
    agent = create_tool_calling_agent(model, tools, prompt)
    executor = AgentExecutor(agent=agent, tools=tools)

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        agent=executor,  # auto_adapt_agent() routes through LangChainBridge
        session_actions=actions,
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
