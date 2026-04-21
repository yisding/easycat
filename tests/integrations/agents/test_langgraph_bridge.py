"""Tests for :class:`LangGraphBridge`.

Uses duck-typed mocks so the real ``langgraph`` package is not required
at test time.  The mock reproduces the tuple shape yielded by
``graph.astream(stream_mode=[...], subgraphs=True)`` and a simplified
``get_state`` / ``update_state`` surface.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import (
    AgentTurnInput,
    BridgeInputError,
    CancellationMode,
    CommitRule,
    RecorderContext,
    UnitKind,
)
from easycat.integrations.agents.langgraph import LangGraphBridge
from easycat.runtime.journal import InMemoryRingBuffer

# ── Mocks ────────────────────────────────────────────────────────


class _MockAIMessageChunk:
    def __init__(self, content: str = "") -> None:
        self.content = content
        self.tool_call_chunks: list[Any] = []
        self.id = "c"
        self.type = "ai"


class _MockMessage:
    def __init__(self, role: str, content: str, message_id: str | None = None) -> None:
        self.type = {"assistant": "ai", "user": "human", "system": "system"}.get(role, role)
        self.content = content
        self.id = message_id


class _MockState:
    def __init__(
        self,
        values: dict[str, Any] | None = None,
        checkpoint_id: str = "cp-1",
        thread_id: str = "t-1",
    ) -> None:
        self.values = values or {}
        self.config = {"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}}
        self.metadata = {"step": 1}
        self.next: tuple[str, ...] = ()
        self.tasks: tuple[Any, ...] = ()
        self.interrupts: tuple[Any, ...] = ()


class _MockCheckpointer:
    """Marker object — LangGraphBridge only probes ``graph.checkpointer``."""


class _MockCompiledGraph:
    """Duck-types ``langgraph.graph.state.CompiledStateGraph``.

    Yields a scripted sequence of tuples matching the
    ``subgraphs=True`` + multi-mode stream shape.
    """

    def __init__(
        self,
        scripted: list[tuple[tuple[str, ...], str, Any]],
        *,
        state: _MockState | None = None,
    ) -> None:
        self._scripted = scripted
        self.checkpointer = _MockCheckpointer()
        self._state = state or _MockState(values={"messages": []})
        self.update_state_calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def astream(
        self,
        input: Any,
        config: dict[str, Any] | None = None,
        *,
        stream_mode: Any,
        subgraphs: bool = False,
    ) -> AsyncIterator[tuple[tuple[str, ...], str, Any]]:
        async def _gen() -> AsyncIterator[tuple[tuple[str, ...], str, Any]]:
            for item in self._scripted:
                yield item

        return _gen()

    def get_state(self, config: dict[str, Any]) -> _MockState:
        return self._state

    def update_state(self, config: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        self.update_state_calls.append((config, values))
        # Simulate ``add_messages``: dedupe by id when present.
        key = "messages"
        existing = list(self._state.values.get(key, []))
        for new_msg in values.get(key, []):
            new_id = getattr(new_msg, "id", None) or (
                new_msg.get("id") if isinstance(new_msg, dict) else None
            )
            if new_id:
                replaced = False
                for i, old in enumerate(existing):
                    old_id = getattr(old, "id", None) or (
                        old.get("id") if isinstance(old, dict) else None
                    )
                    if old_id == new_id:
                        existing[i] = new_msg
                        replaced = True
                        break
                if replaced:
                    continue
            existing.append(new_msg)
        self._state.values[key] = existing
        return {"configurable": {"thread_id": "t-1", "checkpoint_id": "cp-2"}}


def _recorder(journal: InMemoryRingBuffer | None = None) -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


# ── Construction ────────────────────────────────────────────────


class TestLangGraphBridgeConstruction:
    def test_rejects_none(self):
        with pytest.raises(BridgeInputError):
            LangGraphBridge(None)  # type: ignore[arg-type]

    def test_rejects_graph_without_astream(self):
        class NotAGraph:
            pass

        with pytest.raises(BridgeInputError):
            LangGraphBridge(NotAGraph())

    def test_rejects_graph_without_checkpointer(self):
        class GraphNoCP:
            checkpointer = None

            def astream(self, *args: Any, **kwargs: Any) -> Any:
                return iter(())

        with pytest.raises(BridgeInputError):
            LangGraphBridge(GraphNoCP())

    def test_committable_boundaries_published(self):
        assert LangGraphBridge.COMMITTABLE_BOUNDARIES[UnitKind.WORKFLOW_NODE] == (
            CommitRule.BETWEEN_NODES
        )
        assert LangGraphBridge.COMMITTABLE_BOUNDARIES[UnitKind.AGENT] == (CommitRule.BETWEEN_TURNS)


# ── Invoke flow ─────────────────────────────────────────────────


class TestLangGraphBridgeInvoke:
    @pytest.mark.asyncio
    async def test_nodes_produce_workflow_node_cursors_and_handoff(self):
        scripted = [
            ((), "updates", {"research": {"messages": [_MockMessage("assistant", "R")]}}),
            ((), "messages", (_MockAIMessageChunk(content="R text "), {})),
            ((), "updates", {"write": {"messages": [_MockMessage("assistant", "W")]}}),
            ((), "messages", (_MockAIMessageChunk(content="W text"), {})),
        ]
        graph = _MockCompiledGraph(scripted)
        bridge = LangGraphBridge(graph)

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hello"), rec):
            events.append(ev)

        text = "".join(e.text for e in events if e.kind == "text_delta")
        assert text == "R text W text"

        records = journal.read()
        handoffs = [r for r in records if r.name == "framework_handoff"]
        assert len(handoffs) == 1
        assert handoffs[0].data["from_unit"] == "research"
        assert handoffs[0].data["to_unit"] == "write"

        # Cursor stack balanced.
        assert [r.name for r in records].count("unit_entered") == [r.name for r in records].count(
            "unit_exited"
        )

    @pytest.mark.asyncio
    async def test_cancel_token_short_circuits(self):
        scripted = [
            ((), "messages", (_MockAIMessageChunk(content="suppressed"), {})),
        ]
        graph = _MockCompiledGraph(scripted)
        bridge = LangGraphBridge(graph)

        token = CancelToken()
        token.cancel()

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("x"), rec, cancel_token=token):
            events.append(ev)

        assert not any(e.kind == "text_delta" for e in events)
        records = journal.read()
        assert any(r.name == "cancellation_boundary" for r in records)

    @pytest.mark.asyncio
    async def test_checkpoint_snapshot_recorded(self):
        debug_payload = {
            "type": "checkpoint",
            "payload": {
                "config": {"configurable": {"checkpoint_id": "cp-42"}},
            },
        }
        scripted = [
            ((), "debug", debug_payload),
        ]
        graph = _MockCompiledGraph(scripted, state=_MockState(checkpoint_id="cp-final"))
        bridge = LangGraphBridge(graph)

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)
        async for _ in bridge.invoke(AgentTurnInput.from_text("x"), rec):
            pass

        refs = [r.data["state_ref"] for r in journal.read() if r.name == "state_snapshot"]
        assert "langgraph:cp-42" in refs
        # Final-state snapshot also recorded.
        assert any("langgraph:cp-final" == ref for ref in refs)

    @pytest.mark.asyncio
    async def test_internal_sentinel_nodes_filtered(self):
        scripted = [
            ((), "updates", {"__start__": None}),
            ((), "updates", {"planner": {}}),
        ]
        graph = _MockCompiledGraph(scripted)
        bridge = LangGraphBridge(graph)
        rec = _recorder()
        async for _ in bridge.invoke(AgentTurnInput.from_text("x"), rec):
            pass
        # Only the planner cursor should be created.


# ── State / interruption ─────────────────────────────────────────


class TestLangGraphBridgeState:
    def test_snapshot_includes_checkpoint_id(self):
        graph = _MockCompiledGraph([], state=_MockState(checkpoint_id="cp-snap"))
        bridge = LangGraphBridge(graph)
        snap = bridge.snapshot_state()
        assert snap.fields["framework"] == "langgraph"
        assert snap.fields["checkpoint_id"] == "cp-snap"
        assert snap.fields["thread_id"] == bridge._thread_id

    def test_apply_interruption_rewrites_last_ai_via_update_state(self):
        ai_msg = _MockMessage("assistant", "the full reply", message_id="m-ai-1")
        state = _MockState(values={"messages": [_MockMessage("user", "hi"), ai_msg]})
        graph = _MockCompiledGraph([], state=state)
        bridge = LangGraphBridge(graph)

        bridge.apply_interruption("the full", CancellationMode.IMMEDIATE_STOP)
        assert graph.update_state_calls
        cfg, values = graph.update_state_calls[0]
        assert "messages" in values
        # Last AI message in state now truncated.
        assert state.values["messages"][-1].content == "the full..."

    def test_apply_interruption_no_ai_message_is_noop(self):
        state = _MockState(values={"messages": [_MockMessage("user", "hi")]})
        graph = _MockCompiledGraph([], state=state)
        bridge = LangGraphBridge(graph)
        # Should not raise.
        bridge.apply_interruption("something", CancellationMode.IMMEDIATE_STOP)
        assert not graph.update_state_calls

    def test_reset_rotates_thread_id(self):
        graph = _MockCompiledGraph([], state=_MockState())
        bridge = LangGraphBridge(graph, thread_id="original")
        assert bridge._thread_id == "original"
        bridge.reset()
        assert bridge._thread_id != "original"

    def test_append_interruption_note(self):
        graph = _MockCompiledGraph([], state=_MockState(values={"messages": []}))
        bridge = LangGraphBridge(graph)
        bridge.append_interruption_note("user interrupted")
        assert graph.update_state_calls
