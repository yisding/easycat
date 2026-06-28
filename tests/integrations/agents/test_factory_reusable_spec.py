"""Coverage for the VoiceApp cross-connection isolation predicate.

``is_reusable_agent_spec`` decides which high-level ``agent`` values a
per-connection server may forward into a *fresh* ``EasyConfig`` for every
connection. Framework agent objects, built bridges/runners, and unknown objects
can carry mutable runtime state and MUST be rejected so the caller routes them
through a ``config_factory`` instead.

This is the load-bearing guard for the "VoiceApp state isolated across
connections" contract, so every branch is locked here. The SDK-free
``_runnable_pins_conversation`` cases run in CI unconditionally; the
per-framework cases self-skip when the SDK is absent.
"""

from __future__ import annotations

from typing import Any

import pytest

from easycat.integrations.agents._factory import (
    _runnable_pins_conversation,
    is_reusable_agent_spec,
)


class _Workflow:
    async def on_user_turn(self, text: str) -> str:
        return text


# ── SDK-free unit tests for the load-bearing conversation-pin walk ───


class _Binding:
    """``RunnableBinding``-shaped stub. ``_bound_config`` walks ``.config`` /
    ``.bound`` exactly like a real ``with_config(...)`` wrapper, so this locks
    the pin-detection contract without the LangChain SDK in CI."""

    def __init__(self, config: dict[str, Any], bound: Any = None) -> None:
        self.config = config
        self.bound = bound


@pytest.mark.parametrize("key", ["thread_id", "checkpoint_id", "session_id"])
def test_runnable_pinning_a_conversation_key_is_detected(key: str) -> None:
    binding = _Binding({"configurable": {key: "abc"}}, bound=object())
    assert _runnable_pins_conversation(binding) is True


def test_runnable_pin_detected_on_outer_wrapper_over_inner_bound() -> None:
    inner = _Binding({"tags": ["x"]}, bound=object())
    outer = _Binding({"configurable": {"thread_id": "t"}}, bound=inner)
    assert _runnable_pins_conversation(outer) is True


def test_runnable_without_configurable_does_not_pin() -> None:
    assert _runnable_pins_conversation(_Binding({"tags": ["x"]}, bound=object())) is False


def test_runnable_with_empty_or_unrelated_configurable_does_not_pin() -> None:
    assert _runnable_pins_conversation(_Binding({"configurable": {}})) is False
    assert _runnable_pins_conversation(_Binding({"configurable": {"tenant_id": "acme"}})) is False


def test_runnable_with_blank_pin_value_does_not_pin() -> None:
    # A falsy bound value is treated as "not pinned" (the bridge would resolve a
    # fresh conversation), matching ``any(configurable.get(key) for ...)``.
    assert _runnable_pins_conversation(_Binding({"configurable": {"thread_id": ""}})) is False


# ── Framework objects are not reusable across connections ─────────────


def test_openai_agent_is_not_reusable() -> None:
    agents_mod = pytest.importorskip("agents")
    assert is_reusable_agent_spec(agents_mod.Agent(name="t", instructions="hi")) is False


def test_pydantic_ai_agent_is_not_reusable() -> None:
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent as PydanticAgent
    from pydantic_ai.models.test import TestModel

    assert is_reusable_agent_spec(PydanticAgent(TestModel(custom_output_text="ok"))) is False


def test_plain_langchain_runnable_is_not_reusable() -> None:
    rc = pytest.importorskip("langchain_core.runnables")
    assert is_reusable_agent_spec(rc.RunnableLambda(lambda x: x)) is False


def test_langchain_runnable_pinning_conversation_is_rejected() -> None:
    """The load-bearing branch: a runnable bound to one conversation key is
    resolved identically by every per-session bridge, so all concurrent
    connections would share one checkpointer thread / history store. Reject it
    so the caller routes it through a ``config_factory``."""
    rc = pytest.importorskip("langchain_core.runnables")
    runnable = rc.RunnableLambda(lambda x: x)
    for key in ("thread_id", "session_id", "checkpoint_id"):
        pinned = runnable.with_config(configurable={key: "conv-1"})
        assert is_reusable_agent_spec(pinned) is False, key


def test_compiled_langgraph_graph_is_not_reusable() -> None:
    pytest.importorskip("langgraph")
    from typing import TypedDict

    from langgraph.graph import START, StateGraph

    class _S(TypedDict):
        x: int

    builder = StateGraph(_S)
    builder.add_node("noop", lambda s: s)
    builder.add_edge(START, "noop")
    graph = builder.compile()
    assert is_reusable_agent_spec(graph) is False
    pinned = graph.with_config(configurable={"thread_id": "t"})
    assert is_reusable_agent_spec(pinned) is False


def test_llama_workflow_is_not_reusable(fake_workflows_modules: None) -> None:
    # Reuse the suite's ``sys.modules['workflows']`` shim (see conftest) so the
    # real-isinstance ``is_llama_workflow_instance`` check resolves without the
    # workflows SDK's step-validation getting in the way.
    from ._llama_agents_bridge_support import _FakeWorkflowBase

    class _W(_FakeWorkflowBase):
        pass

    assert is_reusable_agent_spec(_W()) is False


# ── Built bridges / runners / unknowns are NOT reusable ──────────────


def test_built_bridge_is_not_reusable() -> None:
    from easycat.integrations.agents.base import ExternalAgentBridge
    from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge

    bridge = GenericWorkflowBridge(workflow=_Workflow())
    assert isinstance(bridge, ExternalAgentBridge)
    assert is_reusable_agent_spec(bridge) is False


def test_agent_runner_is_not_reusable() -> None:
    from easycat.integrations.agents._agent_runner import AgentRunner

    assert is_reusable_agent_spec(AgentRunner(_Workflow())) is False


def test_plain_callable_and_arbitrary_object_are_not_reusable() -> None:
    class _CustomAgent:
        async def run(self, text: str) -> str:
            return text

    assert is_reusable_agent_spec(_CustomAgent()) is False
    assert is_reusable_agent_spec(object()) is False
