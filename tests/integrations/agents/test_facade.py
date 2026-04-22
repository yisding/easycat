"""auto_adapt_agent() bridge selection and error paths."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents._factory import auto_adapt_agent
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    BridgeInputError,
    CancellationMode,
    CommitRule,
    ExternalAgentBridge,
    FrameworkStateSnapshot,
    UnitKind,
)


class _CustomBridge:
    """Minimal ExternalAgentBridge implementation."""

    COMMITTABLE_BOUNDARIES = {UnitKind.AGENT: CommitRule.BETWEEN_TURNS}

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        yield AgentBridgeEvent(kind="text_delta", text="custom")
        yield AgentBridgeEvent(kind="done", text="custom")

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return FrameworkStateSnapshot(fields={}, kind="custom")

    def apply_interruption(self, delivered_text: str, mode: CancellationMode, **_) -> None:
        pass

    def replace_last_assistant_text(self, text: str) -> None:
        pass

    def append_interruption_note(self, note: str) -> None:
        pass

    def reset(self) -> None:
        pass


class TestAutoAdaptWithBridge:
    def test_bridge_passthrough(self):
        bridge = _CustomBridge()
        assert isinstance(bridge, ExternalAgentBridge)
        assert auto_adapt_agent(bridge) is bridge

    def test_unknown_object_passthrough(self):
        obj = object()
        adapted = auto_adapt_agent(obj)
        assert adapted is obj


class TestAutoAdaptBridgeSelection:
    def test_workflow_shallow_routes_to_generic_workflow_bridge(self):
        from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge

        class _Shallow:
            async def on_user_turn(self, text: str) -> str:
                return text

        adapted = auto_adapt_agent(_Shallow())
        assert isinstance(adapted, GenericWorkflowBridge)
        assert not adapted.deep_mode

    def test_workflow_deep_routes_to_generic_workflow_bridge(self):
        from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge

        class _Deep:
            async def on_user_turn(self, text: str, *, recorder=None, cancel_token=None):
                yield f"deep: {text}"

        adapted = auto_adapt_agent(_Deep())
        assert isinstance(adapted, GenericWorkflowBridge)
        assert adapted.deep_mode

    def test_pydantic_graph_raises_bridge_input_error(self):
        pytest.importorskip("pydantic_graph")
        from pydantic_graph import Graph

        with pytest.raises(BridgeInputError, match="PydanticAIBridge"):
            auto_adapt_agent(Graph(nodes=[]))

    def test_realtime_class_name_raises_bridge_input_error(self):
        class RealtimeClient:
            pass

        with pytest.raises(BridgeInputError, match="realtime"):
            auto_adapt_agent(RealtimeClient())

    def test_realtime_method_raises_bridge_input_error(self):
        class _Client:
            def create_realtime_session(self):
                pass

        with pytest.raises(BridgeInputError, match="realtime"):
            auto_adapt_agent(_Client())
