"""Agent-initiated session actions — LangGraph.

Shows the simplest session action that works on every transport: ending
the session after the current reply finishes. The ``end_call`` tool
closes over the module-level :class:`SessionActions` object — LangGraph
tools receive arguments only, so the queue is shared via closure rather
than a deps/context parameter.

For telephony-specific actions (transfer, DTMF, SMS) see
``examples/twilio_app.py``.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart
       uv add easycat[langgraph] langchain-openai
Run:   uv run python examples/session_actions_langgraph.py
"""

try:
    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.prebuilt import create_react_agent
except ImportError as exc:
    raise SystemExit(
        "LangGraph is required. Install with: uv add easycat[langgraph] langchain-openai"
    ) from exc

from easycat import EasyConfig, SessionActions, run

actions = SessionActions()


@tool
def end_call(reason: str = "") -> str:
    """End the call gracefully. Use when the user says goodbye."""
    actions.end_call(reason=reason)
    return "Ending the call now."


graph = create_react_agent(
    ChatOpenAI(model="gpt-5.5"),
    tools=[end_call],
    prompt=(
        "You are a helpful voice assistant. "
        "When the user says goodbye, use the end_call tool. "
        "Be concise — you are speaking, not writing."
    ),
    checkpointer=InMemorySaver(),
)

run(EasyConfig.mic(agent=graph, session_actions=actions))
