"""Agent adapters for third-party AI frameworks."""

from easycat.agents.base import BaseAgentAdapter, serialize_output
from easycat.agents.openai_agents import OpenAIAgentsAdapter
from easycat.agents.pydantic_ai import PydanticAIAdapter

__all__ = [
    "BaseAgentAdapter",
    "OpenAIAgentsAdapter",
    "PydanticAIAdapter",
    "serialize_output",
]
