from __future__ import annotations

import pytest

from easycat.integrations.agents._bridge_adapter_shim import BridgeAdapterShim
from easycat.integrations.agents._factory import auto_adapt_agent


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
    from easycat.integrations.agents._base_adapter import BaseAgentAdapter

    class _FakeAdapter(BaseAgentAdapter):
        async def run(self, text: str) -> str:
            return text

    adapter = _FakeAdapter()
    assert auto_adapt_agent(adapter) is adapter


def test_auto_adapt_agent_wraps_workflow_objects():
    from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge

    adapted = auto_adapt_agent(_Workflow())
    assert isinstance(adapted, BridgeAdapterShim)
    assert isinstance(adapted.bridge, GenericWorkflowBridge)


def test_auto_adapt_agent_wraps_openai_agents():
    agents_mod = pytest.importorskip("agents")
    from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge

    raw = agents_mod.Agent(name="test", instructions="hi")
    adapted = auto_adapt_agent(raw)
    assert isinstance(adapted, BridgeAdapterShim)
    assert isinstance(adapted.bridge, OpenAIAgentsBridge)


def test_auto_adapt_agent_wraps_pydantic_agents():
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent as PydanticAgent

    from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

    raw = PydanticAgent("openai:gpt-4o-mini")
    adapted = auto_adapt_agent(raw)
    assert isinstance(adapted, BridgeAdapterShim)
    assert isinstance(adapted.bridge, PydanticAIBridge)
