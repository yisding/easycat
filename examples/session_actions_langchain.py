"""Agent-initiated session actions — LangChain.

Shows the simplest session action that works on every transport: ending
the session after the current reply finishes. The ``end_call`` tool
closes over the module-level :class:`SessionActions` object — LangChain
tools receive arguments only, so there's no context/deps parameter to
thread through :class:`LangChainBridge`. The queue is shared via
closure rather than a deps/context parameter.

For telephony-specific actions (transfer, DTMF, SMS) see
``examples/twilio_app.py``.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart --group dev
       uv pip install "langchain<1" "langchain-openai<1"
       uv run easycat doctor
       uv run easycat doctor --env-file .env  # if keys live in .env
       uv run easycat doctor --env-file .env --json  # for parseable checks
Run:   uv run python examples/session_actions_langchain.py
       uv run --env-file .env python examples/session_actions_langchain.py  # if keys live in .env

LangChain 1.x removed ``create_tool_calling_agent`` (the recommended
replacement, ``langchain.agents.create_agent``, returns a LangGraph
``CompiledStateGraph``).  This example pins the still-supported 0.3.x
line so the LangChain bridge keeps a runnable demo.
"""

try:
    from langchain.agents import AgentExecutor, create_tool_calling_agent
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI
except ImportError as exc:
    raise SystemExit(
        "LangChain (<1.0) is required. For an app, run: "
        "uv add 'easycat[quickstart]' 'langchain<1' 'langchain-openai<1'. "
        "In this repo, run: uv sync --extra quickstart --group dev; "
        'uv pip install "langchain<1" "langchain-openai<1"'
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
            (
                "You are a helpful voice assistant. "
                "When the user says goodbye, use the end_call tool. "
                "Be concise — you are speaking, not writing."
            ),
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
