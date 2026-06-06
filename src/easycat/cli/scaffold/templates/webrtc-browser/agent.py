"""Browser voice agent built on the OpenAI Agents SDK over WebRTC."""

from agents import Agent, function_tool
from easycat import EasyConfig, run


@function_tool
def connection_help() -> str:
    """Return the local browser URL for this demo."""
    return "Open http://localhost:8080 and allow microphone access."


agent = Agent(name="$AGENT_NAME", instructions="$AGENT_INSTRUCTIONS", tools=[connection_help])
run(EasyConfig.browser(agent=agent, **__EASYCAT_CONFIG_EXTRA__))  # noqa: F821
