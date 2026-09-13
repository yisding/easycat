"""Telnyx phone agent built on the OpenAI Agents SDK."""

from agents import Agent, function_tool

from tools import take_message

AGENT_NAME = "$AGENT_NAME"
INSTRUCTIONS = "$AGENT_INSTRUCTIONS"


def make_agent() -> Agent:
    """Build this project's agent; tests and server.py import it, so keep it side-effect free."""
    return Agent(name=AGENT_NAME, instructions=INSTRUCTIONS, tools=[function_tool(take_message)])
