"""Local voice bot driven by a small PydanticAI workflow.

The EasyCat integration point is the workflow object, not an individual
PydanticAI agent. The workflow decides which specialist handles each turn
and keeps that state across turns.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart --extra pydantic-ai --group dev
       uv run easycat doctor
       uv run easycat doctor --env-file .env  # if keys live in .env
       uv run easycat doctor --env-file .env --json  # for parseable checks
Run:   uv run python examples/pydantic_ai_workflow_voice.py
       uv run --env-file .env python examples/pydantic_ai_workflow_voice.py  # if keys live in .env
"""

try:
    from pydantic_ai import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "PydanticAI is required. For an app, run: "
        "uv add 'easycat[quickstart,pydantic-ai]'. In this repo, run: "
        "uv sync --extra quickstart --extra pydantic-ai --group dev"
    ) from exc

from easycat import EasyConfig, run

TECH_TERMS = ("audio", "browser", "setup", "install")


class SupportWorkflow:
    """Route each turn to a specialist while remembering the active lane."""

    def __init__(self, *, model_name: str = "openai:gpt-5.2") -> None:
        self.active_agent_id = "billing"
        self.billing = Agent(model_name, system_prompt="Help with invoices and plans.")
        self.technical = Agent(model_name, system_prompt="Help with setup and audio.")

    async def on_user_turn(self, text: str) -> str:
        if any(term in text.lower() for term in TECH_TERMS):
            self.active_agent_id = "technical"
        elif "invoice" in text.lower() or "plan" in text.lower():
            self.active_agent_id = "billing"
        agent = self.technical if self.active_agent_id == "technical" else self.billing
        return str((await agent.run(text)).output)


run(EasyConfig.mic(agent=SupportWorkflow()))
