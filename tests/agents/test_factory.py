from __future__ import annotations

import pytest

from easycat.agents import PydanticAIAdapter, PydanticAIWorkflowAdapter
from easycat.agents.factory import auto_adapt_agent


class _CustomAgent:
    async def run(self, text: str) -> str:
        return text


class _Workflow:
    async def on_user_turn(self, text: str) -> str:
        return text


def test_auto_adapt_agent_passthrough_for_unknown_agents():
    agent = _CustomAgent()
    assert auto_adapt_agent(agent) is agent


def test_auto_adapt_agent_keeps_existing_adapter():
    adapter = PydanticAIAdapter(_CustomAgent())
    assert auto_adapt_agent(adapter) is adapter


def test_auto_adapt_agent_wraps_workflow_objects():
    adapted = auto_adapt_agent(_Workflow())
    assert isinstance(adapted, PydanticAIWorkflowAdapter)


def test_auto_adapt_agent_wraps_openai_agents():
    agents_mod = pytest.importorskip("agents")
    from easycat.agents import OpenAIAgentsAdapter

    raw = agents_mod.Agent(name="test", instructions="hi")
    adapted = auto_adapt_agent(raw)
    assert isinstance(adapted, OpenAIAgentsAdapter)


def test_auto_adapt_agent_wraps_pydantic_agents():
    pydantic_ai_mod = pytest.importorskip("pydantic_ai")

    raw = pydantic_ai_mod.Agent("openai:gpt-4o-mini")
    adapted = auto_adapt_agent(raw)
    assert isinstance(adapted, PydanticAIAdapter)
