"""Telnyx phone agent built on the OpenAI Agents SDK."""

from agents import Agent, function_tool


@function_tool
def take_message(name: str, message: str) -> str:
    """Record a caller message for later follow-up."""
    return f"Message saved for {name}: {message}"


def make_agent() -> Agent:
    return Agent(name="$AGENT_NAME", instructions="$AGENT_INSTRUCTIONS", tools=[take_message])
