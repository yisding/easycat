"""AC2.11: auto_adapt_agent() with ExternalAgentBridge detection."""

from __future__ import annotations

from collections.abc import AsyncIterator

from easycat.agents.factory import auto_adapt_agent
from easycat.cancel import CancelToken
from easycat.integrations.agents._bridge_adapter_shim import BridgeAdapterShim
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    CancellationMode,
    CommitRule,
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

    def apply_interruption(self, delivered_text: str, mode: CancellationMode) -> None:
        pass

    def reset(self) -> None:
        pass


class TestAutoAdaptWithBridge:
    def test_bridge_wrapped_in_shim(self):
        bridge = _CustomBridge()
        adapted = auto_adapt_agent(bridge)
        assert isinstance(adapted, BridgeAdapterShim)
        assert adapted.bridge is bridge

    def test_shim_returned_as_is(self):
        bridge = _CustomBridge()
        shim = BridgeAdapterShim(bridge)
        adapted = auto_adapt_agent(shim)
        assert adapted is shim

    def test_unknown_object_passthrough(self):
        obj = object()
        adapted = auto_adapt_agent(obj)
        assert adapted is obj
