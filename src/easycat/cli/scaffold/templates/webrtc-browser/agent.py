"""Browser voice agent built on the OpenAI Agents SDK over WebRTC."""

from agents import Agent, function_tool
from easycat import EasyConfig, run

from tools import connection_help

AGENT_NAME = "$AGENT_NAME"
INSTRUCTIONS = "$AGENT_INSTRUCTIONS"


def make_agent() -> Agent:
    """Build this project's agent; tests import it, so keep it side-effect free."""
    return Agent(
        name=AGENT_NAME, instructions=INSTRUCTIONS, tools=[function_tool(connection_help)]
    )


def make_config() -> EasyConfig:
    """Wire the agent into a browser voice config. No transport opens until run()."""
    return EasyConfig.browser(agent=make_agent(), **__EASYCAT_CONFIG_EXTRA__)  # noqa: F821


if __name__ == "__main__":
    run(make_config())
