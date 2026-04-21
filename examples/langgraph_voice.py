"""Local voice bot demo using a LangGraph ``StateGraph``.

Wraps a compiled LangGraph graph in :class:`LangGraphBridge`.  Node
transitions become ``workflow_node`` cursors in the EasyCat journal,
LangGraph's native ``checkpoint_id`` values are preserved as state
snapshots, and barge-in rewrites the last AI message via
``graph.update_state`` (LangGraph's ``add_messages`` reducer dedupes
by message ``id`` so the edited message replaces in place).

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv add easycat[langgraph] langchain-openai
  uv run python examples/langgraph_voice.py
"""

from __future__ import annotations

import asyncio
from typing import Annotated, TypedDict

from easycat import (
    EasyCatConfig,
    LocalTransportConfig,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)


class State(TypedDict):
    messages: Annotated[list, "add_messages"]


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    try:
        from langchain_core.messages import AIMessage
        from langchain_openai import ChatOpenAI
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.graph.message import add_messages
    except ImportError as exc:
        raise SystemExit(
            "LangGraph is required. Install with: uv add easycat[langgraph] langchain-openai"
        ) from exc

    # Re-declare State with the real add_messages reducer once the
    # import succeeds — the top-level placeholder keeps this file
    # importable without LangGraph installed.
    class _State(TypedDict):
        messages: Annotated[list, add_messages]

    model = ChatOpenAI(model="gpt-4o-mini", api_key=api_key)

    def research(state: _State) -> dict:
        reply = model.invoke(state["messages"])
        return {"messages": [AIMessage(content=f"Research: {reply.content}")]}

    def write(state: _State) -> dict:
        reply = model.invoke(state["messages"])
        return {"messages": [AIMessage(content=reply.content)]}

    graph = (
        StateGraph(_State)
        .add_node("research", research)
        .add_node("write", write)
        .add_edge(START, "research")
        .add_edge("research", "write")
        .add_edge("write", END)
        .compile(checkpointer=InMemorySaver())
    )

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=LocalTransportConfig(),
        agent=graph,  # auto_adapt_agent() routes through LangGraphBridge
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
