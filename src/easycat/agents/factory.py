"""Helpers for normalizing third-party agents to EasyCat adapters."""

from __future__ import annotations

from typing import Any

from easycat.agents.base import BaseAgentAdapter
from easycat.agents.openai_agents import OpenAIAgentsAdapter
from easycat.agents.pydantic_ai import PydanticAIAdapter


def auto_adapt_agent(agent: Any) -> Any:
    """Wrap known third-party agent objects in an EasyCat adapter.

    This provides a lower-friction onramp for users who pass a raw agent
    instance to :func:`easycat.create_session`.

    Supported auto-detected frameworks:
    - ``pydantic_ai.Agent`` -> :class:`PydanticAIAdapter`
    - ``agents.Agent`` (OpenAI Agents SDK) -> :class:`OpenAIAgentsAdapter`

    Unknown agent types are returned unchanged.
    """
    if isinstance(agent, BaseAgentAdapter):
        return agent

    module_name = type(agent).__module__
    if module_name.startswith("pydantic_ai"):
        return PydanticAIAdapter(agent)
    if module_name.startswith("agents"):
        return OpenAIAgentsAdapter(agent)
    return agent

