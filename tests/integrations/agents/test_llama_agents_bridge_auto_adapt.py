"""LlamaAgents auto-adapt tests."""

from __future__ import annotations

from ._llama_agents_bridge_support import (
    LlamaAgentsBridge,
    _LocalWorkflow,
)


class TestAutoAdapt:
    def test_auto_adapt_llama_workflow(self, fake_workflows_modules):
        from easycat.integrations.agents._factory import auto_adapt_agent

        workflow = _LocalWorkflow(result="ok")
        adapted = auto_adapt_agent(workflow)

        assert isinstance(adapted, LlamaAgentsBridge)
