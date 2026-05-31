"""Example L2: LangGraphBridge wrapping a two-node StateGraph.

Mirrors plan appendix Example L2 — a two-node ``StateGraph`` with an
``InMemorySaver`` checkpointer, wrapped via :class:`LangGraphBridge`.
Uses duck-typed mocks so the real ``langgraph`` SDK is not required at
test time.

Events follow the ``astream_events(version="v2")`` shape emitted by a
compiled LangGraph (the graph is itself a LangChain ``Runnable``).
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


def _evt(
    event: str,
    *,
    name: str,
    run_id: str,
    parent: str = "",
    node: str | None = None,
    checkpoint_id: str = "cp-step-1",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {"checkpoint_id": checkpoint_id}
    if node is not None:
        meta["langgraph_node"] = node
        meta["langgraph_checkpoint_ns"] = ""
    return {
        "event": event,
        "name": name,
        "run_id": run_id,
        "parent_ids": [parent] if parent else [],
        "data": data or {},
        "metadata": meta,
    }


class _MockTwoNodeGraph:
    """Duck-types a LangGraph ``CompiledStateGraph`` via ``astream_events``.

    Represents a simple ``research → write`` pipeline.
    """

    def __init__(self) -> None:
        self.checkpointer = object()
        self._state = _MockStateSnapshot("cp-final", [])
        self._get_state_calls = 0
        self.update_state_calls: list[Any] = []

    def astream_events(
        self,
        input: Any,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        async def _gen() -> AsyncIterator[dict[str, Any]]:
            yield _evt("on_chain_start", name="research", run_id="n1", node="research")
            yield _evt(
                "on_chat_model_stream",
                name="ChatOpenAI",
                run_id="m1",
                parent="n1",
                node="research",
                data={"chunk": _MockAIMessageChunk(content="Research summary: ")},
            )
            yield _evt(
                "on_chat_model_stream",
                name="ChatOpenAI",
                run_id="m1",
                parent="n1",
                node="research",
                data={"chunk": _MockAIMessageChunk(content="Paris is the capital.")},
            )
            yield _evt("on_chain_end", name="research", run_id="n1", node="research")
            yield _evt(
                "on_chain_start",
                name="write",
                run_id="n2",
                node="write",
                checkpoint_id="cp-step-2",
            )
            yield _evt(
                "on_chat_model_stream",
                name="ChatOpenAI",
                run_id="m2",
                parent="n2",
                node="write",
                checkpoint_id="cp-step-2",
                data={"chunk": _MockAIMessageChunk(content=" Final write-up complete.")},
            )
            yield _evt(
                "on_chain_end",
                name="write",
                run_id="n2",
                node="write",
                checkpoint_id="cp-step-2",
            )

        return _gen()

    def get_state(self, config: dict[str, Any]) -> _MockStateSnapshot:
        self._get_state_calls += 1
        # First call is the bridge's pre-turn baseline probe — a fresh
        # thread has no prior checkpoint, so report no id.  Later calls
        # return the post-turn final state.
        if self._get_state_calls == 1:
            return _MockStateSnapshot("", [])
        return self._state

    def get_state_history(self, config: dict[str, Any]) -> Any:
        # Real per-super-step checkpoints, newest→oldest, as LangGraph
        # yields them; LangGraph 1.1.x node-event metadata carries no
        # ``checkpoint_id`` so the trail must come from real history.
        return iter(
            [
                _MockStateSnapshot("cp-final", []),
                _MockStateSnapshot("cp-step-2", []),
                _MockStateSnapshot("cp-step-1", []),
            ]
        )

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
    async def test_two_node_graph_journals_handoff_and_yields_text(self):
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

        # Handoffs live in the journal, not on the stream.
        assert not any(e.kind == "handoff" for e in events)
        handoffs = [
            (r.data["from_unit"], r.data["to_unit"])
            for r in journal.read()
            if r.name == "framework_handoff"
        ]
        assert ("research", "write") in handoffs

        # The full per-step checkpoint trail is reconstructed from the
        # checkpointer's real history (fresh thread → all three), in
        # chronological order.
        records = journal.read()
        state_refs = [r.data["state_ref"] for r in records if r.name == "state_snapshot"]
        assert state_refs == [
            "langgraph:cp-step-1",
            "langgraph:cp-step-2",
            "langgraph:cp-final",
        ]

    def test_committable_boundaries_published(self):
        assert LangGraphBridge.COMMITTABLE_BOUNDARIES
