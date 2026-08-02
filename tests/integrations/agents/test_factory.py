from __future__ import annotations

import pytest

import easycat.integrations.agents._factory as agent_factory
from easycat.integrations.agents._factory import auto_adapt_agent
from easycat.integrations.agents.base import BridgeInputError


class _CustomAgent:
    async def run(self, text: str) -> str:
        return text


class _Workflow:
    async def on_user_turn(self, text: str) -> str:
        return text


class _ExtraPositionalWorkflow:
    async def on_user_turn(self, text: str, context: object) -> str:
        return text


class _UnsupportedKeywordWorkflow:
    async def on_user_turn(self, text: str, *, tenant: str) -> str:
        return text


class _FakeGraph:
    def __init__(self) -> None:
        self.applied_config: dict | None = None

    def with_config(self, config: dict):
        self.applied_config = config
        return self


class _FakeBindingBase:
    def __init__(
        self,
        bound,
        *,
        config: dict | None = None,
        kwargs: dict | None = None,
        config_factories: list | None = None,
    ) -> None:
        self.bound = bound
        self.config = config or {}
        self.kwargs = kwargs or {}
        self.config_factories = config_factories or []


class _FakeBinding(_FakeBindingBase):
    pass


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


def test_auto_adapt_agent_consults_builtin_adapters_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    adapted = object()

    def miss(agent, model):
        calls.append(f"miss:{model}")

    def match(agent, model):
        calls.append(f"match:{model}")
        return agent_factory._AdaptedAgent(adapted)

    def should_not_run(agent, model):
        calls.append("late")

    monkeypatch.setattr(
        agent_factory,
        "_BUILTIN_AGENT_ADAPTERS",
        (miss, match, should_not_run),
    )

    assert auto_adapt_agent(object(), model="voice-model") is adapted
    assert calls == ["miss:voice-model", "match:voice-model"]


@pytest.mark.parametrize(
    ("workflow_type", "message"),
    [
        (_ExtraPositionalWorkflow, "2 required positional"),
        (_UnsupportedKeywordWorkflow, "tenant"),
    ],
)
def test_auto_adapt_agent_rejects_uncallable_workflow_signatures(
    workflow_type: type,
    message: str,
) -> None:
    with pytest.raises(BridgeInputError, match=message):
        auto_adapt_agent(workflow_type())


@pytest.fixture
def fake_langgraph_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agent_factory,
        "_langgraph_runtime_types",
        lambda: (_FakeGraph, (_FakeBinding,), (_FakeBindingBase,)),
    )


def test_unwrap_compiled_graph_preserves_binding_only_chain(fake_langgraph_types) -> None:
    graph = _FakeGraph()
    outer = _FakeBinding(_FakeBinding(graph))

    assert agent_factory._unwrap_compiled_state_graph(outer) is outer


def test_unwrap_compiled_graph_restores_peeled_config(fake_langgraph_types) -> None:
    graph = _FakeGraph()
    retry = _FakeBindingBase(
        graph,
        config={"configurable": {"thread_id": "inner", "tenant": "acme"}},
    )
    outer = _FakeBinding(
        retry,
        config={"configurable": {"thread_id": "outer"}, "tags": ["voice"]},
    )

    assert agent_factory._unwrap_compiled_state_graph(outer) is graph
    assert graph.applied_config == {
        "configurable": {"thread_id": "outer", "tenant": "acme"},
        "tags": ["voice"],
    }


def test_unwrap_compiled_graph_rejects_behavior_above_retry(fake_langgraph_types) -> None:
    wrapped = _FakeBindingBase(_FakeGraph(), kwargs={"stop": ["done"]})

    with pytest.raises(BridgeInputError, match="silently dropped"):
        agent_factory._unwrap_compiled_state_graph(wrapped)
