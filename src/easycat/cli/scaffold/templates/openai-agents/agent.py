"""Voice agent built on the OpenAI Agents SDK."""
from agents import Agent, function_tool
from easycat import EasyConfig, run
@function_tool
def current_time() -> str:
    """Return the current local time as HH:MM."""
    from datetime import datetime
    return datetime.now().strftime("%H:%M")
agent = Agent(name="$AGENT_NAME", instructions="$AGENT_INSTRUCTIONS", tools=[current_time])
run(EasyConfig.mic(agent=agent, **__EASYCAT_CONFIG_EXTRA__))  # noqa: F821
