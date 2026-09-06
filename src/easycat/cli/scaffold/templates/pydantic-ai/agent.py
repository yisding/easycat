"""Voice agent built on PydanticAI."""

from easycat import EasyConfig, run
from pydantic_ai import Agent

from tools import current_time

AGENT_NAME = "$AGENT_NAME"
INSTRUCTIONS = "$AGENT_INSTRUCTIONS"


def make_agent() -> Agent:
    """Build this project's agent; tests import it, so keep it side-effect free."""
    return Agent(
        "openai:gpt-4.1-mini", name=AGENT_NAME, system_prompt=INSTRUCTIONS, tools=[current_time]
    )


def make_config() -> EasyConfig:
    """Wire the agent into a voice config. No audio device opens until run()."""
    return EasyConfig.mic(agent=make_agent(), **__EASYCAT_CONFIG_EXTRA__)  # noqa: F821


if __name__ == "__main__":
    run(make_config())
