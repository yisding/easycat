"""Voice agent built on PydanticAI."""

from datetime import datetime

from easycat import EasyConfig, run
from pydantic_ai import Agent

agent = Agent("openai:gpt-4.1-mini", system_prompt="$AGENT_INSTRUCTIONS")


@agent.tool_plain
def current_time() -> str:
    """Return the current local time as HH:MM."""
    return datetime.now().astimezone().strftime("%H:%M")


run(EasyConfig.mic(agent=agent, **__EASYCAT_CONFIG_EXTRA__))  # noqa: F821
