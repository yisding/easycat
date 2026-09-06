"""Voice agent built on the OpenAI Agents SDK."""

from agents import Agent, function_tool
from easycat import VoiceApp

from tools import current_time

AGENT_NAME = "$AGENT_NAME"
INSTRUCTIONS = "$AGENT_INSTRUCTIONS"


def make_agent() -> Agent:
    """Build this project's agent; tests import it, so keep it side-effect free."""
    return Agent(name=AGENT_NAME, instructions=INSTRUCTIONS, tools=[function_tool(current_time)])


def make_app() -> VoiceApp:
    """Wire the agent into a voice app. No audio device opens until run()."""
    return VoiceApp(agent=make_agent(), **__EASYCAT_CONFIG_EXTRA__)  # noqa: F821


if __name__ == "__main__":
    make_app().run("local")
