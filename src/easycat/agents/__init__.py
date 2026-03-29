"""Agent adapters for third-party AI frameworks."""

from easycat.agents.base import BaseAgentAdapter, serialize_output
from easycat.agents.factory import auto_adapt_agent
from easycat.agents.openai_agents import OpenAIAgentsAdapter, build_openai_agents_adapter
from easycat.agents.pydantic_ai import PydanticAIAdapter

__all__ = [
    "BaseAgentAdapter",
    "OpenAIAgentsAdapter",
    "PydanticAIAdapter",
    "auto_adapt_agent",
    "build_openai_agents_adapter",
    "serialize_output",
]
