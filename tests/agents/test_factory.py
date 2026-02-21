from __future__ import annotations

from easycat.agents import OpenAIAgentsAdapter, PydanticAIAdapter
from easycat.agents.factory import auto_adapt_agent


class _CustomAgent:
    async def run(self, text: str) -> str:
        return text


def test_auto_adapt_agent_passthrough_for_unknown_agents():
    agent = _CustomAgent()
    assert auto_adapt_agent(agent) is agent


def test_auto_adapt_agent_keeps_existing_adapter():
    adapter = PydanticAIAdapter(_CustomAgent())
    assert auto_adapt_agent(adapter) is adapter


def test_auto_adapt_agent_wraps_openai_agents_by_module_name():
    OpenAIAgent = type("Agent", (), {"__module__": "agents.core"})
    adapted = auto_adapt_agent(OpenAIAgent())
    assert isinstance(adapted, OpenAIAgentsAdapter)


def test_auto_adapt_agent_wraps_pydantic_agents_by_module_name():
    PydanticAgent = type("Agent", (), {"__module__": "pydantic_ai.agent"})
    adapted = auto_adapt_agent(PydanticAgent())
    assert isinstance(adapted, PydanticAIAdapter)
