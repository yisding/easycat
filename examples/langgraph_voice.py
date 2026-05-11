"""Local voice bot demo using a LangGraph ``StateGraph``.

Wraps a compiled LangGraph graph in :class:`LangGraphBridge`. Node
transitions become ``workflow_node`` cursors in the EasyCat journal,
LangGraph's native ``checkpoint_id`` values are preserved as state
snapshots, and barge-in rewrites the last AI message via
``graph.update_state`` (LangGraph's ``add_messages`` reducer dedupes
by message ``id`` so the edited message replaces in place).

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart
       uv add easycat[langgraph] langchain-openai
Run:   uv run python examples/langgraph_voice.py
"""

from typing import Annotated, TypedDict

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

from easycat import EasyConfig, run


class State(TypedDict):
    messages: Annotated[list, add_messages]


model = ChatOpenAI(model="gpt-5.5")


def research(state: State) -> dict:
    reply = model.invoke(state["messages"])
    return {"messages": [AIMessage(content=f"Research: {reply.content}")]}


def write(state: State) -> dict:
    reply = model.invoke(state["messages"])
    return {"messages": [AIMessage(content=reply.content)]}


graph = (
    StateGraph(State)
    .add_node("research", research)
    .add_node("write", write)
    .add_edge(START, "research")
    .add_edge("research", "write")
    .add_edge("write", END)
    .compile(checkpointer=InMemorySaver())
)

run(EasyConfig.mic(agent=graph))
