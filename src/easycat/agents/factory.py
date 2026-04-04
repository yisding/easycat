"""Helpers for normalizing third-party agents to EasyCat adapters."""

from __future__ import annotations

from typing import Any

from easycat.agents.base import BaseAgentAdapter
from easycat.agents.openai_agents import OpenAIAgentsAdapter
from easycat.agents.pydantic_ai import PydanticAIAdapter
from easycat.agents.pydantic_ai_workflow import PydanticAIWorkflowAdapter


def auto_adapt_agent(agent: Any) -> Any:
    """Wrap known third-party agent objects in an EasyCat adapter.

    This provides a lower-friction onramp for users who pass a raw agent
    instance to :func:`easycat.create_session`.

    Supported auto-detected frameworks:
    - workflow objects with ``on_user_turn(...)`` -> :class:`PydanticAIWorkflowAdapter`
    - ``pydantic_ai.Agent`` -> :class:`PydanticAIAdapter`
    - ``agents.Agent`` (OpenAI Agents SDK) -> :class:`OpenAIAgentsAdapter`

    Unknown agent types are returned unchanged.
    """
    if isinstance(agent, BaseAgentAdapter):
        return agent

    if callable(getattr(agent, "on_user_turn", None)):
        return PydanticAIWorkflowAdapter(agent)

    try:
        from pydantic_ai import Agent as PydanticAgent

        if isinstance(agent, PydanticAgent):
            return PydanticAIAdapter(agent)
    except ImportError:
        pass

    try:
        from agents import Agent as OpenAIAgent  # type: ignore[import-untyped]

        if isinstance(agent, OpenAIAgent):
            return OpenAIAgentsAdapter(agent)
    except ImportError:
        pass

    return agent
