"""Tests for :class:`LangGraphBridge`.

Uses duck-typed mocks so the real ``langgraph`` package is not required
at test time.  The mock reproduces the event shape yielded by
``graph.astream_events(..., version="v2")`` — a compiled LangGraph
graph is itself a LangChain ``Runnable``, so the bridge consumes the
same dict-shaped events as ``LangChainBridge`` plus the LangGraph
``metadata`` fields (``langgraph_node``, ``langgraph_step``,
``langgraph_checkpoint_ns``, ``checkpoint_id``).
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
    """Marker — LangGraphBridge only probes ``graph.checkpointer``."""


class _MockCompiledGraph:
    """Duck-types ``langgraph.graph.state.CompiledStateGraph``.

    Emits scripted ``astream_events(version="v2")`` dicts.  Tests build
    the script directly; helpers below make the common shapes easy.
    """

    def __init__(
        self,
        scripted: list[dict[str, Any]] | None = None,
        *,
        state: _MockState | None = None,
    ) -> None:
        self._scripted = scripted or []
        self.checkpointer = _MockCheckpointer()
        self._state = state or _MockState(values={"messages": []})
        self.update_state_calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def astream_events(
        self,
        input: Any,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        async def _gen() -> AsyncIterator[dict[str, Any]]:
            for event in self._scripted:
                yield event

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


def _node_start(node: str, run_id: str, checkpoint_id: str = "cp-1") -> dict[str, Any]:
    return {
        "event": "on_chain_start",
        "name": node,
        "run_id": run_id,
        "parent_ids": [],
        "data": {},
        "metadata": {
            "langgraph_node": node,
            "langgraph_step": 1,
            "langgraph_checkpoint_ns": "",
            "checkpoint_id": checkpoint_id,
            "thread_id": "t-1",
        },
    }


def _node_end(node: str, run_id: str, checkpoint_id: str = "cp-1") -> dict[str, Any]:
    return {
        "event": "on_chain_end",
        "name": node,
        "run_id": run_id,
        "parent_ids": [],
        "data": {},
        "metadata": {
            "langgraph_node": node,
            "langgraph_checkpoint_ns": "",
            "checkpoint_id": checkpoint_id,
        },
    }


def _model_stream(
    text: str,
    *,
    run_id: str = "m",
    parent: str = "",
    node: str | None = None,
    checkpoint_id: str = "cp-1",
) -> dict[str, Any]:
    meta: dict[str, Any] = {"checkpoint_id": checkpoint_id}
    if node is not None:
        meta["langgraph_node"] = node
    return {
        "event": "on_chat_model_stream",
        "name": "ChatOpenAI",
        "run_id": run_id,
        "parent_ids": [parent] if parent else [],
        "data": {"chunk": _MockAIMessageChunk(content=text)},
        "metadata": meta,
    }


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

    def test_rejects_graph_without_astream_events(self):
        class NotAGraph:
            pass

        with pytest.raises(BridgeInputError):
            LangGraphBridge(NotAGraph())

    def test_rejects_graph_without_checkpointer(self):
        class GraphNoCP:
            checkpointer = None

            def astream_events(self, *args: Any, **kwargs: Any) -> Any:
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
            _node_start("research", "n1"),
            _model_stream("R text ", run_id="m1", parent="n1", node="research"),
            _node_end("research", "n1"),
            _node_start("write", "n2", checkpoint_id="cp-2"),
            _model_stream("W text", run_id="m2", parent="n2", node="write"),
            _node_end("write", "n2", checkpoint_id="cp-2"),
        ]
        graph = _MockCompiledGraph(scripted, state=_MockState(checkpoint_id="cp-2"))
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

        # Workflow nodes created.
        workflow_nodes = [
            r
            for r in records
            if r.name == "unit_entered" and r.data["unit_kind"] == "workflow_node"
        ]
        assert {r.data["display_name"] for r in workflow_nodes} == {"research", "write"}

    @pytest.mark.asyncio
    async def test_cancel_token_short_circuits(self):
        scripted = [_model_stream("suppressed", run_id="m")]
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
    async def test_checkpoint_snapshot_recorded_from_event_metadata(self):
        """Checkpoint ids arrive inline on event metadata; the final
        ``get_state`` snapshot adds the post-turn checkpoint once."""
        scripted = [
            _node_start("planner", "n1", checkpoint_id="cp-mid"),
            _node_end("planner", "n1", checkpoint_id="cp-mid"),
        ]
        graph = _MockCompiledGraph(scripted, state=_MockState(checkpoint_id="cp-final"))
        bridge = LangGraphBridge(graph)

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)
        async for _ in bridge.invoke(AgentTurnInput.from_text("x"), rec):
            pass

        refs = [r.data["state_ref"] for r in journal.read() if r.name == "state_snapshot"]
        assert "langgraph:cp-mid" in refs
        assert "langgraph:cp-final" in refs
        # Dedupe: cp-mid appears on both node_start and node_end events
        # but is recorded only once.
        assert refs.count("langgraph:cp-mid") == 1

    @pytest.mark.asyncio
    async def test_turn_context_prepended_to_messages_input(self):
        """Per-turn system/developer context must be forwarded into the
        graph's ``messages`` input so messages-state graphs see
        session-provided instructions (caller-id, system prefix, etc.).
        Filtering out user/assistant items avoids duplicating state that
        the graph's checkpointer already owns."""

        captured: dict[str, Any] = {}

        class _CapturingGraph(_MockCompiledGraph):
            def astream_events(
                self,
                input: Any,
                **kwargs: Any,
            ) -> AsyncIterator[dict[str, Any]]:
                captured["input"] = input
                return super().astream_events(input, **kwargs)

        graph = _CapturingGraph([_node_start("p", "n1"), _node_end("p", "n1")])
        bridge = LangGraphBridge(graph)
        turn = AgentTurnInput.from_text(
            "ping",
            context=[
                {"role": "system", "content": "Caller id: +15551234"},
                {"role": "user", "content": "should be dropped"},
            ],
        )
        async for _ in bridge.invoke(turn, _recorder()):
            pass
        messages = captured["input"]["messages"]
        # System message survived; caller-provided user message was dropped.
        assert messages == [
            ("system", "Caller id: +15551234"),
            ("user", "ping"),
        ]

    @pytest.mark.asyncio
    async def test_non_node_chain_events_ignored(self):
        """``on_chain_start`` without a matching ``langgraph_node`` (e.g.
        internal runnables inside a node) shouldn't open a cursor."""
        scripted = [
            # Internal RunnableSequence inside a node — name ≠ langgraph_node.
            {
                "event": "on_chain_start",
                "name": "RunnableSequence",
                "run_id": "r1",
                "parent_ids": [],
                "data": {},
                "metadata": {"langgraph_node": "planner"},
            },
            _node_start("planner", "n1"),
            _node_end("planner", "n1"),
        ]
        graph = _MockCompiledGraph(scripted)
        bridge = LangGraphBridge(graph)

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)
        async for _ in bridge.invoke(AgentTurnInput.from_text("x"), rec):
            pass

        workflow_nodes = [
            r
            for r in journal.read()
            if r.name == "unit_entered" and r.data["unit_kind"] == "workflow_node"
        ]
        assert len(workflow_nodes) == 1
        assert workflow_nodes[0].data["display_name"] == "planner"


# ── State / interruption ─────────────────────────────────────────


class TestLangGraphBridgeState:
    def test_snapshot_includes_checkpoint_id(self):
        graph = _MockCompiledGraph(state=_MockState(checkpoint_id="cp-snap"))
        bridge = LangGraphBridge(graph)
        snap = bridge.snapshot_state()
        assert snap.fields["framework"] == "langgraph"
        assert snap.fields["checkpoint_id"] == "cp-snap"
        assert snap.fields["thread_id"] == bridge._thread_id

    def test_apply_interruption_rewrites_last_ai_via_update_state(self):
        ai_msg = _MockMessage("assistant", "the full reply", message_id="m-ai-1")
        state = _MockState(values={"messages": [_MockMessage("user", "hi"), ai_msg]})
        graph = _MockCompiledGraph(state=state)
        bridge = LangGraphBridge(graph)

        bridge.apply_interruption("the full", CancellationMode.IMMEDIATE_STOP)
        assert graph.update_state_calls
        cfg, values = graph.update_state_calls[0]
        assert "messages" in values
        # Last AI message in state now truncated.
        assert state.values["messages"][-1].content == "the full..."

    def test_apply_interruption_no_ai_message_is_noop(self):
        state = _MockState(values={"messages": [_MockMessage("user", "hi")]})
        graph = _MockCompiledGraph(state=state)
        bridge = LangGraphBridge(graph)
        bridge.apply_interruption("something", CancellationMode.IMMEDIATE_STOP)
        assert not graph.update_state_calls

    def test_reset_rotates_thread_id(self):
        graph = _MockCompiledGraph(state=_MockState())
        bridge = LangGraphBridge(graph, thread_id="original")
        assert bridge._thread_id == "original"
        bridge.reset()
        assert bridge._thread_id != "original"

    def test_append_interruption_note(self):
        graph = _MockCompiledGraph(state=_MockState(values={"messages": []}))
        bridge = LangGraphBridge(graph)
        bridge.append_interruption_note("user interrupted")
        assert graph.update_state_calls

    @pytest.mark.asyncio
    async def test_get_stream_writer_custom_event_yields_text_delta(self):
        """``get_stream_writer`` writes land as ``("custom", payload)``
        tuples on the top-level graph's ``on_chain_stream``.  Payloads
        with a ``text`` field should drive TTS; opaque telemetry
        payloads should stay silent."""
        graph_chunk_text = {
            "event": "on_chain_stream",
            "name": "LangGraph",
            "run_id": "g1",
            "data": {"chunk": ("custom", {"text": "Looking that up..."})},
            "metadata": {},
        }
        graph_chunk_telemetry = {
            "event": "on_chain_stream",
            "name": "LangGraph",
            "run_id": "g1",
            "data": {"chunk": ("custom", {"progress": 0.5})},
            "metadata": {},
        }
        graph_chunk_plain_string = {
            "event": "on_chain_stream",
            "name": "LangGraph",
            "run_id": "g1",
            "data": {"chunk": ("custom", "plain status")},
            "metadata": {},
        }
        scripted = [
            _node_start("planner", "n1"),
            graph_chunk_text,
            graph_chunk_telemetry,
            graph_chunk_plain_string,
            _node_end("planner", "n1"),
        ]
        graph = _MockCompiledGraph(scripted)
        bridge = LangGraphBridge(graph)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("x"), _recorder()):
            events.append(ev)

        text_deltas = [e.text for e in events if e.kind == "text_delta"]
        assert "Looking that up..." in text_deltas
        assert "plain status" in text_deltas
        assert all("progress" not in t for t in text_deltas)

    @pytest.mark.asyncio
    async def test_interrupt_via_updates_channel_raises(self):
        """``interrupt()`` lands as ``("updates", {"__interrupt__": (...)})``
        on the top-level graph's ``on_chain_stream`` when
        ``stream_mode=["updates"]`` is passed to ``astream_events``.
        Voice runtimes cannot resume HITL, so the bridge fails loudly."""

        class _MockInterrupt:
            def __init__(self, value: Any) -> None:
                self.value = value
                self.id = "irq-1"

        graph_chunk = {
            "event": "on_chain_stream",
            "name": "LangGraph",
            "run_id": "g1",
            "data": {
                "chunk": (
                    "updates",
                    {"__interrupt__": (_MockInterrupt("approve?"),)},
                )
            },
            "metadata": {},
        }
        scripted = [_node_start("planner", "n1"), graph_chunk]
        graph = _MockCompiledGraph(scripted)
        bridge = LangGraphBridge(graph)

        with pytest.raises(BridgeInputError, match="interrupt"):
            async for _ in bridge.invoke(AgentTurnInput.from_text("x"), _recorder()):
                pass

    @pytest.mark.asyncio
    async def test_post_stream_pending_interrupt_raises(self):
        """If a graph stops with pending interrupts but the ``updates``
        channel didn't surface them (older LangGraph, custom checkpointer),
        the post-stream ``state.tasks[i].interrupts`` sweep should still
        flag the HITL mismatch."""

        class _Interrupt:
            def __init__(self, value: Any) -> None:
                self.value = value

        class _Task:
            def __init__(self, interrupts: tuple[Any, ...]) -> None:
                self.interrupts = interrupts

        state = _MockState(values={"messages": []}, checkpoint_id="cp-paused")
        state.tasks = (_Task((_Interrupt("review?"),)),)
        graph = _MockCompiledGraph([_node_start("p", "n1"), _node_end("p", "n1")], state=state)
        bridge = LangGraphBridge(graph)

        with pytest.raises(BridgeInputError, match="interrupt"):
            async for _ in bridge.invoke(AgentTurnInput.from_text("x"), _recorder()):
                pass

    @pytest.mark.asyncio
    async def test_custom_messages_key_surfaces_final_output(self):
        """When the graph's state schema uses a non-default messages key,
        the end-of-turn ``done.structured_output`` must still be the last
        message in that key rather than silently dropping to ``None``."""
        ai_msg = _MockMessage("assistant", "final reply", message_id="m-1")
        state = _MockState(
            values={"chat_history": [_MockMessage("user", "hi"), ai_msg]},
            checkpoint_id="cp-final",
        )
        scripted = [
            _node_start("chat", "n1"),
            _model_stream("final reply", run_id="m1", parent="n1", node="chat"),
            _node_end("chat", "n1"),
        ]
        graph = _MockCompiledGraph(scripted, state=state)
        bridge = LangGraphBridge(graph, messages_key="chat_history")

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)

        done = [e for e in events if e.kind == "done"]
        assert done and done[0].structured_output is ai_msg
