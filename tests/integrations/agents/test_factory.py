from __future__ import annotations

import pytest

from easycat.integrations.agents._factory import auto_adapt_agent


class _CustomAgent:
    async def run(self, text: str) -> str:
        return text


class _Workflow:
    async def on_user_turn(self, text: str) -> str:
        return text


def test_auto_adapt_agent_returns_plain_run_agents_unchanged():
    # Plain ``async run(text)`` agents are returned as-is so that
    # ``create_session`` can apply ``config.agent_runner`` / ``wrap_agent``
    # rather than being silently pre-wrapped with default config.
    agent = _CustomAgent()
    adapted = auto_adapt_agent(agent)
    assert adapted is agent


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
    from pydantic_ai.models.test import TestModel

    from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

    raw = PydanticAgent(TestModel(custom_output_text="ok"))
    adapted = auto_adapt_agent(raw)
    assert isinstance(adapted, PydanticAIBridge)


def test_auto_adapt_agent_bridge_passthrough():
    from easycat.integrations.agents.base import ExternalAgentBridge
    from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge

    bridge = GenericWorkflowBridge(workflow=_Workflow())
    assert isinstance(bridge, ExternalAgentBridge)
    assert auto_adapt_agent(bridge) is bridge


def test_auto_adapt_agent_runner_wrapping_raw_framework_adapts_inner():
    from easycat.integrations.agents._agent_runner import AgentRunner
    from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge

    inner = _Workflow()
    runner = AgentRunner(inner)
    assert runner._agent is inner
    adapted = auto_adapt_agent(runner)
    assert adapted is runner
    assert isinstance(runner._agent, GenericWorkflowBridge)
    assert runner._is_bridge is True
