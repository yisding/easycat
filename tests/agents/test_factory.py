from __future__ import annotations

import pytest

from easycat.integrations.agents._factory import auto_adapt_agent


class _CustomAgent:
    async def run(self, text: str) -> str:
        return text


class _Workflow:
    async def on_user_turn(self, text: str) -> str:
        return text


def test_auto_adapt_agent_wraps_simple_run_agents_in_agent_runner():
    from easycat.integrations.agents._agent_runner import AgentRunner

    agent = _CustomAgent()
    adapted = auto_adapt_agent(agent)
    assert isinstance(adapted, AgentRunner)
    assert adapted._agent is agent


def test_auto_adapt_agent_wraps_workflow_objects():
    from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge

    adapted = auto_adapt_agent(_Workflow())
    assert isinstance(adapted, GenericWorkflowBridge)


def test_auto_adapt_agent_wraps_openai_agents():
    agents_mod = pytest.importorskip("agents")
    from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge

    raw = agents_mod.Agent(name="test", instructions="hi")
    adapted = auto_adapt_agent(raw)
    assert isinstance(adapted, OpenAIAgentsBridge)


def test_auto_adapt_agent_wraps_pydantic_agents():
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent as PydanticAgent

    from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

    raw = PydanticAgent("openai:gpt-4o-mini")
    adapted = auto_adapt_agent(raw)
    assert isinstance(adapted, PydanticAIBridge)


def test_auto_adapt_agent_bridge_passthrough():
    from easycat.integrations.agents.base import ExternalAgentBridge
    from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge

    bridge = GenericWorkflowBridge(workflow=_Workflow())
    assert isinstance(bridge, ExternalAgentBridge)
    assert auto_adapt_agent(bridge) is bridge
