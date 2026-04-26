"""Agent-initiated session actions — OpenAI Agents SDK.

The same ``SessionActions`` instance is shared between the agent's
context (so tools can enqueue actions) and ``EasyCatConfig`` (so the
session drains them). For telephony actions (transfer, DTMF, SMS) see
``examples/twilio_app.py``.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart
Run:   uv run python examples/session_actions_openai.py
"""

try:
    from agents import Agent, RunContextWrapper, function_tool  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. Install with: uv sync --extra quickstart"
    ) from exc

from easycat import EasyCatConfig, SessionActions, run
from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge

actions = SessionActions()


@function_tool
def end_call(ctx: RunContextWrapper[SessionActions], reason: str = "") -> str:
    """End the call gracefully. Use when the user says goodbye."""
    ctx.context.end_call(reason=reason)
    return "Ending the call now."


run(
    EasyCatConfig.mic(
        agent=OpenAIAgentsBridge(
            agent=Agent(
                name="Assistant",
                instructions=(
                    "You are a helpful voice assistant. "
                    "When the user says goodbye, use the end_call tool. "
                    "Be concise — you are speaking, not writing."
                ),
                tools=[end_call],
            ),
            context=actions,
        ),
        session_actions=actions,
    )
)
