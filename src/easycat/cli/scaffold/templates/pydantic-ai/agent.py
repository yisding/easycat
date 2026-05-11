"""Voice agent built on PydanticAI — local mic to speaker."""

from pydantic_ai import Agent

from easycat import EasyConfig, run

voice_agent = Agent(
    "openai:gpt-4.1-mini",
    system_prompt="$AGENT_INSTRUCTIONS",
)


@voice_agent.tool_plain
def current_time() -> str:
    """Return the current local time as HH:MM."""
    from datetime import datetime

    return datetime.now().strftime("%H:%M")


run(EasyConfig(agent=voice_agent, **__EASYCAT_CONFIG_EXTRA__))  # noqa: F821
