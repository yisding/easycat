"""Example 2: PydanticAIBridge wrapping a pydantic_graph.Graph.

Mirrors plan appendix Example 2 — two-node graph with shared state and
the ``_easycat_event_handler`` convention for deep per-agent capture.
Uses duck-typed mocks so ``pydantic_graph`` is not required.

This fixture runs end-to-end using mock objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import AgentTurnInput, RecorderContext
from easycat.integrations.agents.pydantic_ai import PydanticAIBridge
from easycat.runtime.journal import InMemoryRingBuffer

# ── Mock PydanticAI event types ──────────────────────────────────


class TextPartDelta:
    """Duck-types as ``pydantic_ai.messages.TextPartDelta``."""

    def __init__(self, content_delta: str) -> None:
        self.content_delta = content_delta


class PartDeltaEvent:
    """Duck-types as ``pydantic_ai.agent.PartDeltaEvent``."""

    def __init__(self, delta: Any) -> None:
        self.delta = delta


# ── Mock Graph objects ───────────────────────────────────────────


class _MockGraphNode:
    def __init__(self, name: str) -> None:
        self.__class__ = type(name, (), {})  # type: ignore[assignment]


class _MockGraphRun:
    def __init__(
        self,
        nodes: list[_MockGraphNode],
        result: Any = None,
        history: list[Any] | None = None,
    ) -> None:
        self._nodes = nodes
        self.result = result
        self.history = history

    async def __aenter__(self) -> _MockGraphRun:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def __aiter__(self) -> _MockGraphRun:
        self._iter = iter(self._nodes)
        return self

    async def __anext__(self) -> _MockGraphNode:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _MockGraph:
    def __init__(
        self,
        nodes: list[_MockGraphNode],
        result: Any = None,
        history: list[Any] | None = None,
    ) -> None:
        self._run = _MockGraphRun(nodes, result=result, history=history)

    def iter(self, initial_node: Any, *, state: Any = None) -> _MockGraphRun:
        return self._run


@dataclass
class WorkflowState:
    research_bullets: str = ""
    final_text: str = ""
    _easycat_event_handler: Any = None


# ── Tests ────────────────────────────────────────────────────────


def _recorder(journal: InMemoryRingBuffer | None = None) -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


class TestPydanticAIGraphExample:
    """Plan appendix Example 2 — PydanticAIBridge Graph mode."""

    @pytest.mark.asyncio
    async def test_two_node_graph_produces_workflow_node_cursors(self):
        """ResearchNode → WriteNode produces two workflow_node enters."""
        node_a = _MockGraphNode("ResearchNode")
        node_b = _MockGraphNode("WriteNode")

        graph = _MockGraph([node_a, node_b], history=[node_a, node_b])

        def state_factory() -> WorkflowState:
            return WorkflowState()

        def initial_node_factory(text: str, state: Any) -> _MockGraphNode:
            return node_a

        bridge = PydanticAIBridge(
            graph=graph,
            state_factory=state_factory,
            initial_node_factory=initial_node_factory,
        )

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        # Patch graph.iter to mark handler as called (convention check).
        original_iter = graph.iter

        def patched_iter(initial_node: Any, *, state: Any = None) -> _MockGraphRun:
            handler = getattr(state, "_easycat_event_handler", None)
            if handler is not None:
                handler._was_called = True
            return original_iter(initial_node, state=state)

        graph.iter = patched_iter

        async for _ in bridge.invoke(AgentTurnInput.from_text("research AI trends"), rec):
            pass

        records = journal.read()
        enters = [r for r in records if r.name == "unit_entered"]
        assert len(enters) == 2
        assert enters[0].data["display_name"] == "ResearchNode"
        assert enters[1].data["display_name"] == "WriteNode"

    @pytest.mark.asyncio
    async def test_handoff_triple_between_graph_nodes(self):
        """Graph transition produces exit → handoff → enter triple."""
        node_a = _MockGraphNode("ResearchNode")
        node_b = _MockGraphNode("WriteNode")

        graph = _MockGraph([node_a, node_b], history=[node_a, node_b])

        bridge = PydanticAIBridge(
            graph=graph,
            state_factory=WorkflowState,
            initial_node_factory=lambda text, state: node_a,
        )

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        original_iter = graph.iter

        def patched_iter(initial_node: Any, *, state: Any = None) -> _MockGraphRun:
            handler = getattr(state, "_easycat_event_handler", None)
            if handler is not None:
                handler._was_called = True
            return original_iter(initial_node, state=state)

        graph.iter = patched_iter

        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            pass

        records = journal.read()
        names = [r.name for r in records]

        assert "framework_handoff" in names
        handoff_idx = names.index("framework_handoff")
        handoff_data = records[handoff_idx].data
        assert handoff_data["from_unit"] == "ResearchNode"
        assert handoff_data["to_unit"] == "WriteNode"

        # Verify ordering: exit < handoff < enter.
        exit_indices = [i for i, n in enumerate(names) if n == "unit_exited"]
        enter_indices = [i for i, n in enumerate(names) if n == "unit_entered"]
        first_exit = exit_indices[0]
        second_enter = enter_indices[1]
        assert first_exit < handoff_idx < second_enter

    @pytest.mark.asyncio
    async def test_history_artifact_recorded(self):
        """Graph run produces a history snapshot in the journal."""
        node_a = _MockGraphNode("ResearchNode")
        node_b = _MockGraphNode("WriteNode")

        graph = _MockGraph(
            [node_a, node_b],
            history=[node_a, node_b],
        )

        bridge = PydanticAIBridge(
            graph=graph,
            state_factory=WorkflowState,
            initial_node_factory=lambda text, state: node_a,
        )

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        original_iter = graph.iter

        def patched_iter(initial_node: Any, *, state: Any = None) -> _MockGraphRun:
            handler = getattr(state, "_easycat_event_handler", None)
            if handler is not None:
                handler._was_called = True
            return original_iter(initial_node, state=state)

        graph.iter = patched_iter

        async for _ in bridge.invoke(AgentTurnInput.from_text("go"), rec):
            pass

        records = journal.read()
        snapshot_records = [r for r in records if r.name == "state_snapshot"]
        assert len(snapshot_records) >= 1

    def test_snapshot_state_reports_graph_mode(self):
        node_a = _MockGraphNode("ResearchNode")
        graph = _MockGraph([node_a])

        bridge = PydanticAIBridge(
            graph=graph,
            state_factory=WorkflowState,
            initial_node_factory=lambda text, state: node_a,
        )

        snap = bridge.snapshot_state()
        assert snap.kind == "pydantic_ai_graph"
