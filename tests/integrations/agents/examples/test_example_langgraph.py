"""Example L2: LangGraphBridge wrapping a two-node StateGraph.

Mirrors plan appendix Example L2 — a two-node ``StateGraph`` with an
``InMemorySaver`` checkpointer, wrapped via :class:`LangGraphBridge`.
Uses duck-typed mocks so the real ``langgraph`` SDK is not required at
test time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import AgentTurnInput, RecorderContext
from easycat.integrations.agents.langgraph import LangGraphBridge
from easycat.runtime.journal import InMemoryRingBuffer


class _MockAIMessageChunk:
    def __init__(self, content: str = "") -> None:
        self.content = content
        self.tool_call_chunks: list[Any] = []
        self.type = "ai"


class _MockStateSnapshot:
    def __init__(self, checkpoint_id: str, messages: list[Any]) -> None:
        self.values = {"messages": messages}
        self.config = {"configurable": {"checkpoint_id": checkpoint_id, "thread_id": "t"}}
        self.metadata = {"step": 2}
        self.next: tuple[str, ...] = ()


class _MockTwoNodeGraph:
    """Duck-types a LangGraph ``CompiledStateGraph``.

    Represents a simple ``research → write`` pipeline.
    """

    def __init__(self) -> None:
        self.checkpointer = object()
        self._state = _MockStateSnapshot("cp-final", [])
        self.update_state_calls: list[Any] = []

    def astream(
        self,
        input: Any,
        config: dict[str, Any] | None = None,
        *,
        stream_mode: Any,
        subgraphs: bool = False,
    ) -> AsyncIterator[tuple[tuple[str, ...], str, Any]]:
        async def _gen() -> AsyncIterator[tuple[tuple[str, ...], str, Any]]:
            yield ((), "updates", {"research": {}})
            yield (
                (),
                "messages",
                (_MockAIMessageChunk(content="Research summary: "), {}),
            )
            yield (
                (),
                "messages",
                (_MockAIMessageChunk(content="Paris is the capital."), {}),
            )
            yield ((), "updates", {"write": {}})
            yield (
                (),
                "messages",
                (_MockAIMessageChunk(content=" Final write-up complete."), {}),
            )
            yield (
                (),
                "debug",
                {
                    "type": "checkpoint",
                    "payload": {
                        "config": {"configurable": {"checkpoint_id": "cp-step-1"}},
                    },
                },
            )

        return _gen()

    def get_state(self, config: dict[str, Any]) -> _MockStateSnapshot:
        return self._state

    def update_state(self, config: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        self.update_state_calls.append((config, values))
        return config


def _recorder(journal: InMemoryRingBuffer | None = None) -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


class TestLangGraphExample:
    @pytest.mark.asyncio
    async def test_two_node_graph_yields_handoff_and_text(self):
        graph = _MockTwoNodeGraph()
        bridge = LangGraphBridge(graph, display_name="ResearchWriteGraph")

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("capital of france?"), rec):
            events.append(ev)

        text = "".join(e.text for e in events if e.kind == "text_delta")
        assert "Paris" in text
        assert "Final write-up" in text

        handoffs = [e for e in events if e.kind == "handoff"]
        assert len(handoffs) == 1
        assert handoffs[0].from_unit == "research"
        assert handoffs[0].to_unit == "write"

        # Journal invariants.
        records = journal.read()
        state_refs = [r.data["state_ref"] for r in records if r.name == "state_snapshot"]
        assert "langgraph:cp-step-1" in state_refs
        # Final state snapshot also captured.
        assert any("langgraph:cp-final" == ref for ref in state_refs)

    def test_committable_boundaries_published(self):
        assert LangGraphBridge.COMMITTABLE_BOUNDARIES
