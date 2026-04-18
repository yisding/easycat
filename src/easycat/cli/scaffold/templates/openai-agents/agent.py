"""Voice agent built on the OpenAI Agents SDK — local mic to speaker."""

from datetime import datetime

from agents import Agent, function_tool

from easycat import EasyCatConfig, run


@function_tool
def current_time() -> str:
    """Return the current local time as HH:MM."""
    return datetime.now().strftime("%H:%M")


run(
    EasyCatConfig(
        agent=Agent(
            name="$AGENT_NAME",
            instructions="$AGENT_INSTRUCTIONS",
            tools=[current_time],
        ),
    )
)
