"""Agent-initiated session actions — PydanticAI.

PydanticAI tools access ``SessionActions`` through their deps object.
For telephony actions (transfer, DTMF, SMS) see ``examples/twilio_app.py``.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart; uv add easycat[pydantic-ai]
Run:   uv run python examples/session_actions_pydantic.py
"""

from dataclasses import dataclass

try:
    from pydantic_ai import Agent, RunContext  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit("PydanticAI is required. Install with: uv add easycat[pydantic-ai]") from exc

from easycat import EasyCatConfig, SessionActions, run
from easycat.integrations.agents.pydantic_ai import PydanticAIBridge


@dataclass
class Deps:
    actions: SessionActions


actions = SessionActions()
deps = Deps(actions=actions)

voice_agent = Agent(
    "openai:gpt-5.2",
    deps_type=Deps,
    system_prompt=(
        "You are a helpful voice assistant. "
        "When the user says goodbye, use the end_call tool. "
        "Be concise — you are speaking, not writing."
    ),
)


@voice_agent.tool
def end_call(ctx: RunContext[Deps], reason: str = "") -> str:  # type: ignore[type-arg]
    """End the call gracefully. Use when the user says goodbye."""
    ctx.deps.actions.end_call(reason=reason)
    return "Ending the call now."


run(
    EasyCatConfig.mic(
        agent=PydanticAIBridge(agent=voice_agent, deps=deps),
        session_actions=actions,
    )
)
