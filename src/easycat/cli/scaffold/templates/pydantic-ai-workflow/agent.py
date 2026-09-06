"""Voice workflow with two PydanticAI specialists — local mic to speaker."""

from easycat import EasyConfig, run
from pydantic_ai import Agent

from tools import pick_specialist

PROMPT = "$AGENT_INSTRUCTIONS"


class SupportWorkflow:
    """Routes each turn to a specialist agent; tests build it directly."""

    def __init__(self, specialists: dict[str, Agent]) -> None:
        self.specialists = specialists

    async def on_user_turn(self, text: str) -> str:
        key = pick_specialist(text)
        return str((await self.specialists[key].run(text)).output)


def make_specialists() -> dict[str, Agent]:
    """Build this workflow's specialist agents; tests import it, so keep it side-effect free."""
    return {
        "billing": Agent(
            "openai:gpt-4.1-mini", system_prompt=f"{PROMPT} Handle invoices and plans."
        ),
        "technical": Agent(
            "openai:gpt-4.1-mini", system_prompt=f"{PROMPT} Handle setup and audio."
        ),
    }


def make_workflow() -> SupportWorkflow:
    """Build this project's workflow; tests import it, so keep it side-effect free."""
    return SupportWorkflow(make_specialists())


def make_config() -> EasyConfig:
    """Wire the workflow into a voice config. No audio device opens until run()."""
    return EasyConfig.mic(agent=make_workflow(), **__EASYCAT_CONFIG_EXTRA__)  # noqa: F821


if __name__ == "__main__":
    run(make_config())
