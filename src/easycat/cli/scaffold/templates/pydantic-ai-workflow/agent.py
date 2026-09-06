"""Voice workflow with two PydanticAI specialists — local mic to speaker."""

from easycat import EasyConfig, run
from pydantic_ai import Agent
from pydantic_ai.models import Model

from tools import pick_specialist

PROMPT = "$AGENT_INSTRUCTIONS"
MODEL = "openai:gpt-4.1-mini"


class SupportWorkflow:
    """Routes each turn to a specialist agent; tests build it directly."""

    def __init__(self, specialists: dict[str, Agent]) -> None:
        self.specialists = specialists

    async def on_user_turn(self, text: str) -> str:
        key = pick_specialist(text)
        return str((await self.specialists[key].run(text)).output)


def make_specialists(model: Model | str = MODEL) -> dict[str, Agent]:
    """Build this workflow's specialists; keep it side-effect free.

    PydanticAI resolves ``model`` here, inside ``Agent(...)``, so tests inject a stub.
    """
    return {
        "billing": Agent(model, system_prompt=f"{PROMPT} Handle invoices and plans."),
        "technical": Agent(model, system_prompt=f"{PROMPT} Handle setup and audio."),
    }


def make_workflow(model: Model | str = MODEL) -> SupportWorkflow:
    """Build this project's workflow; tests import it, so keep it side-effect free."""
    return SupportWorkflow(make_specialists(model))


def make_config() -> EasyConfig:
    """Wire the workflow into a voice config. No audio device opens until run()."""
    return EasyConfig.mic(agent=make_workflow(), **__EASYCAT_CONFIG_EXTRA__)  # noqa: F821


if __name__ == "__main__":
    run(make_config())
