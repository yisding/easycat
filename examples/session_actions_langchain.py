"""Agent-initiated session actions — LangChain.

Shows the simplest session action that works on every transport: ending
the session after the current reply finishes. The ``end_call`` tool
closes over the module-level :class:`SessionActions` object — LangChain
tools receive arguments only, so there's no context/deps parameter to
thread through :class:`LangChainBridge`. The queue is shared via
closure rather than a deps/context parameter.

For telephony-specific actions (transfer, DTMF, SMS) see
``examples/twilio_app.py``.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart
       uv add easycat[langchain] langchain langchain-openai
Run:   uv run python examples/session_actions_langchain.py
"""

try:
    from langchain.agents import AgentExecutor, create_tool_calling_agent
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI
except ImportError as exc:
    raise SystemExit(
        "LangChain is required. Install with: uv add easycat[langchain] langchain langchain-openai"
    ) from exc

from easycat import EasyConfig, SessionActions, run

actions = SessionActions()


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
tools = [end_call]
executor = AgentExecutor(
    agent=create_tool_calling_agent(ChatOpenAI(model="gpt-5.5"), tools, prompt),
    tools=tools,
)

run(EasyConfig.mic(agent=executor, session_actions=actions))
