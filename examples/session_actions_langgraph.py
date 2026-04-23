"""Session actions demo — LangGraph.

Shows the simplest session action that works on every transport: ending
the session after the current reply finishes.  The ``end_call`` tool
closes over the module-level :class:`SessionActions` object — LangGraph
tools receive arguments only, so there's no context/deps parameter to
thread through :class:`LangGraphBridge`.

For telephony-specific actions such as transfer, DTMF, and SMS, use the
Twilio example instead: ``examples/twilio_app.py``.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv add easycat[langgraph] langchain-openai
  uv run python examples/session_actions_langgraph.py
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
        from langchain_core.tools import tool
        from langchain_openai import ChatOpenAI
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.prebuilt import create_react_agent
    except ImportError as exc:
        raise SystemExit(
            "LangGraph is required. Install with: uv add easycat[langgraph] langchain-openai"
        ) from exc

    @tool
    def end_call(reason: str = "") -> str:
        """End the call gracefully. Use when the user says goodbye."""
        actions.end_call(reason=reason)
        return "Ending the call now."

    model = ChatOpenAI(model="gpt-4o-mini", api_key=api_key)
    graph = create_react_agent(
        model,
        tools=[end_call],
        prompt=(
            "You are a helpful voice assistant. "
            "When the user says goodbye, use the end_call tool. "
            "Be concise — you are speaking, not writing."
        ),
        checkpointer=InMemorySaver(),
    )

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        agent=graph,  # auto_adapt_agent() routes through LangGraphBridge
        session_actions=actions,
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
