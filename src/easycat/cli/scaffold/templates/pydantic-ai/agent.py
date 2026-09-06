"""Voice agent built on PydanticAI."""

from easycat import EasyConfig, run
from pydantic_ai import Agent
from pydantic_ai.models import Model

from tools import current_time

AGENT_NAME = "$AGENT_NAME"
INSTRUCTIONS = "$AGENT_INSTRUCTIONS"
MODEL = "openai:gpt-4.1-mini"


def make_agent(model: Model | str = MODEL) -> Agent:
    """Build this project's agent; tests import it, so keep it side-effect free.

    PydanticAI resolves ``model`` here, inside ``Agent(...)``, so tests inject a stub.
    """
    return Agent(model, name=AGENT_NAME, system_prompt=INSTRUCTIONS, tools=[current_time])


def make_config() -> EasyConfig:
    """Wire the agent into a voice config. No audio device opens until run()."""
    return EasyConfig.mic(agent=make_agent(), **__EASYCAT_CONFIG_EXTRA__)  # noqa: F821


if __name__ == "__main__":
    run(make_config())
