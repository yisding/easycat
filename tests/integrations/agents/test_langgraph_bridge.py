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

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents._agent_runner import AgentRunner, AgentRunnerConfig
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
from easycat.timeouts import AgentTimeoutError

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
        state_history: list[_MockState] | None = None,
    ) -> None:
        self._scripted = scripted or []
        self.checkpointer = _MockCheckpointer()
        self._state = state or _MockState(values={"messages": []})
        # ``get_state_history`` payload, newest→oldest (as real
        # LangGraph yields).  Mutable so multi-turn tests can grow it
        # between invocations.  Defaults to just the final state.
        self.state_history = state_history
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

    def get_state_history(self, config: dict[str, Any]) -> Any:
        history = self.state_history if self.state_history is not None else [self._state]
        return iter(list(history))

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


def _node_start(
    node: str, run_id: str, checkpoint_id: str = "cp-1", step: int = 1
) -> dict[str, Any]:
    return {
        "event": "on_chain_start",
        "name": node,
        "run_id": run_id,
        "parent_ids": [],
        "data": {},
        "metadata": {
            "langgraph_node": node,
            "langgraph_step": step,
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

    def test_rejects_graph_with_false_checkpointer(self):
        """``graph.compile(checkpointer=False)`` disables persistence and
        sets ``graph.checkpointer`` to ``False`` (not ``None``), but
        ``get_state()`` / ``update_state()`` still raise.  The bridge
        must reject it at construction the same as a missing one."""

        class GraphFalseCP:
            checkpointer = False

            def astream_events(self, *args: Any, **kwargs: Any) -> Any:
                return iter(())

        with pytest.raises(BridgeInputError):
            LangGraphBridge(GraphFalseCP())

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
            # Sequential successor runs in the next super-step.
            _node_start("write", "n2", checkpoint_id="cp-2", step=2),
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
    async def test_agent_runner_timeout_closes_open_cursors(self):
        """The default ``AgentRunner`` enforces its timeout by
        cancelling the bridge's pending ``__anext__``
        (``asyncio.CancelledError``) and then ``aclose()``-ing it
        (``GeneratorExit``).  Neither is an ``Exception``, so the
        ``except Exception`` cleanup is skipped — open workflow/model
        and agent cursors must still get ``unit_exited`` records so the
        recorder's stack invariant holds."""

        class _HangingGraph(_MockCompiledGraph):
            def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
                async def _gen() -> AsyncIterator[dict[str, Any]]:
                    yield _node_start("research", "n1")
                    yield {
                        "event": "on_chat_model_start",
                        "name": "ChatOpenAI",
                        "run_id": "m1",
                        "parent_ids": ["n1"],
                        "data": {},
                        "metadata": {"langgraph_node": "research", "checkpoint_id": "cp-1"},
                    }
                    yield _model_stream("partial ", run_id="m1", parent="n1", node="research")
                    await asyncio.sleep(999)
                    yield _node_end("research", "n1")  # pragma: no cover

                return _gen()

        graph = _HangingGraph(state=_MockState(values={"messages": []}))
        bridge = LangGraphBridge(graph)
        runner = AgentRunner(bridge, AgentRunnerConfig(timeout=0.05))
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        with pytest.raises(AgentTimeoutError):
            async for _ in runner.invoke(AgentTurnInput.from_text("hi"), rec):
                pass

        names = [r.name for r in journal.read()]
        # agent + research workflow_node + model cursors, all paired.
        assert names.count("unit_entered") == names.count("unit_exited") == 3

    @pytest.mark.asyncio
    async def test_parallel_nodes_do_not_violate_recorder_stack(self):
        """A ``StateGraph`` fan-out can start two parallel nodes (each
        invoking a model) before either finishes, so ``on_chain_end`` /
        ``on_chat_model_end`` events can arrive while a sibling cursor
        is still on the recorder's stack top.  The bridge defers each
        non-top close until the obstructing sibling(s) end so the
        recorder's strict LIFO invariant is preserved."""
        scripted = [
            _node_start("research", "n-a"),
            _node_start("write", "n-b"),
            {
                "event": "on_chat_model_start",
                "name": "ChatOpenAI",
                "run_id": "m-a",
                "parent_ids": ["n-a"],
                "data": {},
                "metadata": {"langgraph_node": "research", "checkpoint_id": "cp-1"},
            },
            {
                "event": "on_chat_model_start",
                "name": "ChatOpenAI",
                "run_id": "m-b",
                "parent_ids": ["n-b"],
                "data": {},
                "metadata": {"langgraph_node": "write", "checkpoint_id": "cp-1"},
            },
            _model_stream("A", run_id="m-a", parent="n-a", node="research"),
            _model_stream("B", run_id="m-b", parent="n-b", node="write"),
            # ``m-a`` and ``n-a`` end first, while ``n-b`` / ``m-b`` are
            # still on the stack — naive close would raise
            # ``RecorderInvariantError``.
            {
                "event": "on_chat_model_end",
                "name": "ChatOpenAI",
                "run_id": "m-a",
                "parent_ids": ["n-a"],
                "data": {"output": _MockAIMessageChunk(content="A")},
                "metadata": {"langgraph_node": "research", "checkpoint_id": "cp-1"},
            },
            _node_end("research", "n-a"),
            {
                "event": "on_chat_model_end",
                "name": "ChatOpenAI",
                "run_id": "m-b",
                "parent_ids": ["n-b"],
                "data": {"output": _MockAIMessageChunk(content="B")},
                "metadata": {"langgraph_node": "write", "checkpoint_id": "cp-1"},
            },
            _node_end("write", "n-b"),
        ]
        graph = _MockCompiledGraph(scripted)
        bridge = LangGraphBridge(graph)

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            events.append(ev)

        text = "".join(e.text for e in events if e.kind == "text_delta")
        assert text == "AB"

        records = journal.read()
        names = [r.name for r in records]
        # Agent + 2 nodes + 2 models all entered and exited — no raises.
        assert names.count("unit_entered") == names.count("unit_exited") == 5
        # ``research`` and ``write`` fan out in the *same* super-step;
        # they share a parent namespace but have no edge between them, so
        # no ``research → write`` handoff must be invented.
        assert [r for r in records if r.name == "framework_handoff"] == []

    @pytest.mark.asyncio
    async def test_fanout_join_records_step_crossing_handoffs_only(self):
        """A fan-out (``a`` → parallel ``b``, ``c``) followed by a join
        (``d``) must not invent a ``b → c`` handoff between the parallel
        siblings (same super-step, no edge), while the real edges that
        cross super-steps still record handoffs."""
        scripted = [
            _node_start("a", "n-a", step=1),
            _node_end("a", "n-a"),
            # Fan-out: b and c run together in super-step 2.
            _node_start("b", "n-b", step=2),
            _node_start("c", "n-c", step=2),
            _node_end("b", "n-b"),
            _node_end("c", "n-c"),
            # Join in super-step 3.
            _node_start("d", "n-d", step=3),
            _node_end("d", "n-d"),
        ]
        graph = _MockCompiledGraph(scripted)
        bridge = LangGraphBridge(graph)

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            events.append(ev)

        pairs = [
            (r.data["from_unit"], r.data["to_unit"])
            for r in journal.read()
            if r.name == "framework_handoff"
        ]
        # a→b crosses step 1→2 (real edge); b→c is the same-step sibling
        # pair and must be suppressed; the surviving fan-out node → d
        # crosses step 2→3 (real edge).
        assert ("b", "c") not in pairs
        assert ("a", "b") in pairs
        assert ("c", "d") in pairs
        # The bridge-level handoff events mirror the journal records.
        ev_pairs = [(e.from_unit, e.to_unit) for e in events if e.kind == "handoff"]
        assert ev_pairs == pairs

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
    async def test_checkpoint_trail_recorded_from_real_history(self):
        """LangGraph 1.1.x node events carry ``langgraph_step`` but no
        ``checkpoint_id``, so the per-step trail is reconstructed from
        the checkpointer's real ``get_state_history`` after the turn.
        Across turns the bridge remembers its prior-turn final
        checkpoint and walks history back only to that boundary, so
        each turn records exactly its own checkpoints — once, in
        chronological order — without re-recording earlier turns or
        paying an extra pre-turn ``get_state`` round-trip."""
        scripted = [_node_start("planner", "n1"), _node_end("planner", "n1")]
        graph = _MockCompiledGraph(
            scripted,
            state=_MockState(checkpoint_id="cp-prev"),
            state_history=[_MockState(checkpoint_id="cp-prev")],
        )
        bridge = LangGraphBridge(graph)

        # Turn 1: fresh thread (no prior baseline) → records its lone
        # checkpoint and remembers it as the next turn's baseline.
        j1 = InMemoryRingBuffer(capacity=1000)
        async for _ in bridge.invoke(AgentTurnInput.from_text("x"), _recorder(j1)):
            pass
        refs1 = [r.data["state_ref"] for r in j1.read() if r.name == "state_snapshot"]
        assert refs1 == ["langgraph:cp-prev"]

        # Turn 2: history has grown (newest→oldest); a checkpoint id
        # repeats and the prior-turn baseline (cp-prev) plus anything
        # older must be excluded.
        graph._state = _MockState(checkpoint_id="cp-final")
        graph.state_history = [
            _MockState(checkpoint_id="cp-final"),
            _MockState(checkpoint_id="cp-final"),
            _MockState(checkpoint_id="cp-mid"),
            _MockState(checkpoint_id="cp-prev"),
            _MockState(checkpoint_id="cp-older"),
        ]
        j2 = InMemoryRingBuffer(capacity=1000)
        async for _ in bridge.invoke(AgentTurnInput.from_text("y"), _recorder(j2)):
            pass
        refs2 = [r.data["state_ref"] for r in j2.read() if r.name == "state_snapshot"]
        assert refs2 == ["langgraph:cp-mid", "langgraph:cp-final"]

    @pytest.mark.asyncio
    async def test_checkpoint_trail_iterates_history_lazily(self):
        """``get_state_history`` may be backed by a persistent/remote
        checkpointer that fetches each checkpoint lazily.  The trail walk
        must stop at the prior-turn baseline instead of materializing the
        whole thread, so a long/resumed thread pays O(this turn) — not
        O(total history) — fetches and memory every turn."""
        consumed: list[str] = []

        class _LazyHistoryGraph(_MockCompiledGraph):
            def get_state_history(self, config: dict[str, Any]) -> Any:
                def _gen() -> Any:
                    for st in self.state_history or []:
                        consumed.append(st.config["configurable"]["checkpoint_id"])
                        yield st

                return _gen()

        # newest → oldest: this turn's 2 new checkpoints, the prior-turn
        # baseline, then a long tail that must never be fetched.
        history = [
            _MockState(checkpoint_id="cp-final"),
            _MockState(checkpoint_id="cp-mid"),
            _MockState(checkpoint_id="cp-prev"),
            *(_MockState(checkpoint_id=f"old-{i}") for i in range(1000)),
        ]
        graph = _LazyHistoryGraph(
            [_node_start("p", "n1"), _node_end("p", "n1")],
            state=_MockState(checkpoint_id="cp-final"),
            state_history=history,
        )
        bridge = LangGraphBridge(graph)
        bridge._last_checkpoint_id = "cp-prev"  # prior-turn baseline

        j = InMemoryRingBuffer(capacity=1000)
        async for _ in bridge.invoke(AgentTurnInput.from_text("y"), _recorder(j)):
            pass

        refs = [r.data["state_ref"] for r in j.read() if r.name == "state_snapshot"]
        assert refs == ["langgraph:cp-mid", "langgraph:cp-final"]
        # Only this turn's 2 checkpoints + the baseline were pulled from
        # the lazy iterator; the 1000-entry tail behind it was not.
        assert consumed == ["cp-final", "cp-mid", "cp-prev"]

    @pytest.mark.asyncio
    async def test_turn_context_prepended_to_messages_input(self):
        """Per-turn system/developer context must be forwarded into the
        graph's ``messages`` input so messages-state graphs see
        session-provided instructions (caller-id, system prefix, etc.).
        Filtering out user/assistant items avoids duplicating state that
        the graph's checkpointer already owns.  The injected context
        carries a stable ``id`` so it can be removed afterwards (see
        ``test_transient_context_purged_after_turn``)."""

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
        # System message survived (as an id-bearing dict so it can later
        # be removed); caller-provided user message was dropped.
        assert len(messages) == 2
        ctx_msg = messages[0]
        assert ctx_msg["role"] == "system"
        assert ctx_msg["content"] == "Caller id: +15551234"
        assert ctx_msg["id"].startswith("easycat-ctx-")
        assert messages[1] == {"role": "user", "content": "ping"}

    @pytest.mark.asyncio
    async def test_transient_context_purged_after_turn(self):
        """The per-turn system/developer context is *transient* — leaving
        it in the ``messages`` state would let ``add_messages`` checkpoint
        a fresh copy every turn.  After the turn the bridge must delete it
        from graph state by id so it doesn't accumulate / leak forward."""
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
            "hi",
            context=[{"role": "system", "content": "Caller id: +15551234"}],
        )
        async for _ in bridge.invoke(turn, _recorder()):
            pass

        injected_id = captured["input"]["messages"][0]["id"]
        # The bridge issued an update_state carrying a removal marker for
        # exactly the injected id.
        assert graph.update_state_calls
        _cfg, values = graph.update_state_calls[-1]
        removals = values["messages"]

        def _id_of(m: Any) -> Any:
            return getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)

        assert [_id_of(m) for m in removals] == [injected_id]
        # ``_purge_transient_context`` emits ``RemoveMessage`` when
        # ``langchain-core`` is importable and id-bearing dict markers
        # otherwise.  The ``dev`` group omits ``langchain-core`` (the
        # rest of this suite is duck-typed and runs after a bare
        # ``uv sync --group dev``), so assert whichever shape this
        # environment produced rather than hard-importing.
        try:
            from langchain_core.messages import RemoveMessage
        except ImportError:
            assert all(
                isinstance(m, dict) and m.get("role") == "system" and not m.get("content")
                for m in removals
            )
        else:
            assert all(isinstance(m, RemoveMessage) for m in removals)
        # No context to forward → nothing to purge → no update_state call.
        graph2 = _MockCompiledGraph([_node_start("p", "n1"), _node_end("p", "n1")])
        bridge2 = LangGraphBridge(graph2)
        async for _ in bridge2.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            pass
        assert graph2.update_state_calls == []

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

    @pytest.mark.asyncio
    async def test_state_fetch_failure_does_not_replay_previous_turn(self):
        """A non-streaming graph whose ``get_state()`` fails on a later
        turn (transient/custom checkpointer error) must not surface the
        *previous* turn's final ``AIMessage`` as this turn's
        ``done.text``/``structured_output``.  The stale tail is cleared
        at turn start, so the fallback degrades to this turn's output
        instead of speaking the prior reply again."""

        class _FlakyGraph(_MockCompiledGraph):
            def __init__(self, scripted: list[dict[str, Any]], *, state: _MockState) -> None:
                super().__init__(scripted, state=state)
                self._get_state_calls = 0

            def get_state(self, config: dict[str, Any]) -> _MockState:
                self._get_state_calls += 1
                if self._get_state_calls >= 2:
                    raise RuntimeError("checkpointer unavailable")
                return self._state

        ai_msg = _MockMessage("assistant", "first turn reply", message_id="m-1")
        state = _MockState(
            values={"messages": [_MockMessage("user", "hi"), ai_msg]},
            checkpoint_id="cp-final",
        )
        scripted = [_node_start("answer", "n1"), _node_end("answer", "n1")]
        graph = _FlakyGraph(scripted, state=state)
        bridge = LangGraphBridge(graph)

        # Turn 1 succeeds and captures the final AIMessage.
        done1 = [
            e
            async for e in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder())
            if e.kind == "done"
        ]
        assert done1 and done1[0].text == "first turn reply"
        assert done1[0].structured_output is ai_msg

        # Turn 2: get_state() raises.  Must NOT replay turn 1's reply.
        done2 = [
            e
            async for e in bridge.invoke(AgentTurnInput.from_text("again"), _recorder())
            if e.kind == "done"
        ]
        assert done2 and done2[0].text == ""
        assert done2[0].structured_output is None


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

    @pytest.mark.asyncio
    async def test_non_streaming_node_text_falls_back_to_final_message(self):
        """A node that writes a final ``AIMessage`` to state without
        streaming chat-model tokens (synchronous LLM call, transformed
        model output, plain ``RunnableLambda`` node) leaves
        ``accumulated`` empty.  ``done.text`` must fall back to the
        final message's text so Session can still speak the reply."""
        ai_msg = _MockMessage("assistant", "the actual reply", message_id="m-1")
        state = _MockState(
            values={"messages": [_MockMessage("user", "hi"), ai_msg]},
            checkpoint_id="cp-final",
        )
        scripted = [
            _node_start("answer", "n1"),
            _node_end("answer", "n1"),
        ]
        graph = _MockCompiledGraph(scripted, state=state)
        bridge = LangGraphBridge(graph)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)

        text_deltas = [e for e in events if e.kind == "text_delta"]
        done = [e for e in events if e.kind == "done"]
        assert text_deltas == []  # node did not stream
        assert done and done[0].text == "the actual reply"
        assert done[0].structured_output is ai_msg

    @pytest.mark.asyncio
    async def test_plain_runnable_node_chain_stream_reaches_translator(self):
        """A LangGraph node that is a plain ``RunnableLambda`` (no chat
        model) surfaces its text only through node-level
        ``on_chain_stream``.  Under LangGraph the outermost
        ``on_chain_start`` is the graph itself, so the translator's
        LangChain root-chain dedup must NOT drop the node stream just
        because its run id differs from the graph's."""
        scripted = [
            # The graph's outermost chain start (no parent) — without the
            # LangGraph opt-out this becomes the dedup's root run id.
            {
                "event": "on_chain_start",
                "name": "LangGraph",
                "run_id": "graph",
                "parent_ids": [],
                "data": {},
                "metadata": {},
            },
            {
                "event": "on_chain_start",
                "name": "echo",
                "run_id": "n1",
                "parent_ids": ["graph"],
                "data": {},
                "metadata": {"langgraph_node": "echo", "langgraph_step": 1},
            },
            {
                "event": "on_chain_stream",
                "name": "echo",
                "run_id": "n1",
                "parent_ids": ["graph"],
                "data": {"chunk": "hello from node"},
                "metadata": {"langgraph_node": "echo"},
            },
            {
                "event": "on_chain_end",
                "name": "echo",
                "run_id": "n1",
                "parent_ids": ["graph"],
                "data": {},
                "metadata": {"langgraph_node": "echo"},
            },
        ]
        graph = _MockCompiledGraph(scripted)
        bridge = LangGraphBridge(graph)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("x"), _recorder()):
            events.append(ev)

        text_deltas = [e.text for e in events if e.kind == "text_delta"]
        assert text_deltas == ["hello from node"]

    @pytest.mark.asyncio
    async def test_transformed_final_message_overrides_streamed_model_text(self):
        """A node may stream a child model call and then write a
        *transformed* ``AIMessage`` to state
        (``AIMessage(content=f"Final: {reply.content}")``).  The raw
        model tokens still stream live (speculative streaming can't be
        un-spoken), but ``done.text``/``structured_output`` must record
        the graph's actual final message, not the internal model
        output."""
        final_msg = _MockMessage("assistant", "Final: Hello world", message_id="m-1")
        state = _MockState(
            values={"messages": [_MockMessage("user", "hi"), final_msg]},
            checkpoint_id="cp-final",
        )
        scripted = [
            _node_start("answer", "n1"),
            _model_stream("Hello ", run_id="m1", parent="n1", node="answer"),
            _model_stream("world", run_id="m1", parent="n1", node="answer"),
            _node_end("answer", "n1"),
        ]
        graph = _MockCompiledGraph(scripted, state=state)
        bridge = LangGraphBridge(graph)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)

        # The raw model tokens streamed live and were already spoken;
        # the transformed final message is NOT re-emitted as a delta
        # (that would double-speak).
        text_deltas = [e.text for e in events if e.kind == "text_delta"]
        assert text_deltas == ["Hello ", "world"]
        done = [e for e in events if e.kind == "done"]
        assert done and done[0].text == "Final: Hello world"
        assert done[0].structured_output is final_msg

    @pytest.mark.asyncio
    async def test_done_text_is_empty_when_tail_is_not_ai_message(self):
        """A graph that completes without appending an assistant
        message — e.g. a conditional path returning ``{}`` or an edge
        straight to END — leaves the user's own HumanMessage as the
        messages tail.  ``done.text`` must stay empty so TTS doesn't
        parrot the caller back at them."""
        user_msg = _MockMessage("user", "what time is it?")
        state = _MockState(values={"messages": [user_msg]}, checkpoint_id="cp-final")
        scripted = [
            _node_start("router", "n1"),
            _node_end("router", "n1"),
        ]
        graph = _MockCompiledGraph(scripted, state=state)
        bridge = LangGraphBridge(graph)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("what time is it?"), _recorder()):
            events.append(ev)

        text_deltas = [e for e in events if e.kind == "text_delta"]
        done = [e for e in events if e.kind == "done"]
        assert text_deltas == []
        assert done and done[0].text == ""
        # structured_output still reflects the (non-AI) tail so callers
        # introspecting the raw graph state aren't surprised.
        assert done[0].structured_output is user_msg

    @pytest.mark.asyncio
    async def test_custom_chunk_then_final_ai_message_both_spoken(self):
        """A graph that narrates progress via ``get_stream_writer({"text":
        ...})`` and then writes its real answer as a final ``AIMessage``
        without streaming model tokens must speak *both*: the progress
        chunk leaves ``accumulated`` non-empty, but the final answer must
        still be emitted (not dropped because ``accumulated`` is truthy)."""
        ai_msg = _MockMessage("assistant", "Here is the answer.", message_id="m-1")
        state = _MockState(
            values={"messages": [_MockMessage("user", "hi"), ai_msg]},
            checkpoint_id="cp-final",
        )
        custom_chunk = {
            "event": "on_chain_stream",
            "name": "LangGraph",
            "run_id": "g1",
            "data": {"chunk": ("custom", {"text": "Looking that up... "})},
            "metadata": {},
        }
        scripted = [
            _node_start("plan", "n1"),
            custom_chunk,
            _node_end("plan", "n1"),
        ]
        graph = _MockCompiledGraph(scripted, state=state)
        bridge = LangGraphBridge(graph)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)

        text_deltas = [e.text for e in events if e.kind == "text_delta"]
        done = [e for e in events if e.kind == "done"]
        assert text_deltas == ["Looking that up... ", "Here is the answer."]
        assert done and done[0].text == "Looking that up... Here is the answer."
        assert done[0].structured_output is ai_msg

    @pytest.mark.asyncio
    async def test_default_include_types_surface_non_chat_llm(self):
        """A node that calls a non-chat ``BaseLLM`` only emits
        ``on_llm_*`` events.  The default ``include_types`` must keep
        ``llm`` so the answer isn't filtered out before translation —
        otherwise the turn ends silent with an empty ``done.text``."""

        class _GenerationChunk:
            def __init__(self, text: str) -> None:
                self.text = text

        captured: dict[str, Any] = {}

        class _CapturingGraph(_MockCompiledGraph):
            def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
                captured["kwargs"] = kwargs
                return super().astream_events(input, **kwargs)

        scripted = [
            _node_start("answer", "n1"),
            {
                "event": "on_llm_stream",
                "name": "OpenAI",
                "run_id": "l1",
                "parent_ids": ["n1"],
                "data": {"chunk": _GenerationChunk("completion text")},
                "metadata": {"langgraph_node": "answer", "checkpoint_id": "cp-1"},
            },
            _node_end("answer", "n1"),
        ]
        graph = _CapturingGraph(scripted)
        bridge = LangGraphBridge(graph)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)

        assert "llm" in captured["kwargs"]["include_types"]
        text = "".join(e.text for e in events if e.kind == "text_delta")
        assert text == "completion text"

    @pytest.mark.asyncio
    async def test_parallel_siblings_parented_to_agent_not_each_other(self):
        """During a fan-out two top-level sibling nodes are open at once.
        Each must be parented to the agent cursor (not to the previously
        opened sibling), and a model running inside one sibling must be
        parented to *that* sibling — driven by the event ``parent_ids``,
        not the open-cursor stack top."""
        scripted = [
            _node_start("research", "n-a"),  # parent_ids=[]
            _node_start("write", "n-b"),  # parent_ids=[] (sibling, still open)
            {
                "event": "on_chat_model_start",
                "name": "ChatOpenAI",
                "run_id": "m-b",
                "parent_ids": ["n-b"],
                "data": {},
                "metadata": {"langgraph_node": "write", "checkpoint_id": "cp-1"},
            },
            {
                "event": "on_chat_model_end",
                "name": "ChatOpenAI",
                "run_id": "m-b",
                "parent_ids": ["n-b"],
                "data": {"output": _MockAIMessageChunk(content="W")},
                "metadata": {"langgraph_node": "write", "checkpoint_id": "cp-1"},
            },
            _node_end("write", "n-b"),
            _node_end("research", "n-a"),
        ]
        graph = _MockCompiledGraph(scripted)
        bridge = LangGraphBridge(graph)

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)
        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            pass

        entered = {
            r.data["display_name"]: r.data for r in journal.read() if r.name == "unit_entered"
        }
        agent_id = entered[bridge._display_name]["unit_id"]
        # Both siblings hang off the agent — not off each other.
        assert entered["research"]["parent_unit_id"] == agent_id
        assert entered["write"]["parent_unit_id"] == agent_id
        # The model started while both siblings were open is parented to
        # the sibling its parent_ids points at (write = node-n-b).
        assert entered["ChatOpenAI"]["parent_unit_id"] == "node-n-b"


# ── Partial-turn preservation + reducer guard ────────────────────


def _id_of(m: Any) -> Any:
    return getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)


def _content(m: Any) -> Any:
    return m.get("content") if isinstance(m, dict) else getattr(m, "content", None)


class LastValue:
    """Duck-types LangGraph's plain (no-reducer) ``LastValue`` channel.

    Named exactly ``LastValue`` because the bridge positively
    identifies a no-reducer channel by ``type(channel).__name__``.
    """


class _ReducerChannel:
    """Duck-types an ``Annotated[list, add_messages]`` reducer channel."""

    def __init__(self) -> None:
        self.operator = lambda a, b: a + b


class TestLangGraphBridgePartialTurnOnCancel:
    """A turn cancelled mid-stream (timeout / barge-in ``aclose()``)
    never lets its node return, so the partial assistant output the
    caller already heard is missing from the checkpoint.  The bridge
    must commit it so a follow-up ``apply_interruption()`` truncates
    *this* turn rather than corrupting the previous one."""

    @pytest.mark.asyncio
    async def test_partial_committed_then_interruption_truncates_this_turn(self):
        class _HangingGraph(_MockCompiledGraph):
            def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
                async def _gen() -> AsyncIterator[dict[str, Any]]:
                    yield _node_start("answer", "n1")
                    yield _model_stream("partial reply", run_id="m1", parent="n1", node="answer")
                    await asyncio.sleep(999)
                    yield _node_end("answer", "n1")  # pragma: no cover

                return _gen()

        prior_ai = _MockMessage("assistant", "previous turn", message_id="prev")
        graph = _HangingGraph(state=_MockState(values={"messages": [prior_ai]}))
        bridge = LangGraphBridge(graph)
        runner = AgentRunner(bridge, AgentRunnerConfig(timeout=0.05))

        with pytest.raises(AgentTimeoutError):
            async for _ in runner.invoke(AgentTurnInput.from_text("hi"), _recorder()):
                pass

        # Partial output landed in graph state as the new last AI message.
        msgs = graph._state.values["messages"]
        assert msgs[0] is prior_ai
        assert _content(msgs[-1]) == "partial reply"
        assert msgs[-1] is not prior_ai

        # The interruption rewrite now targets *this* turn, not the
        # previous one.
        bridge.apply_interruption("partial reply", CancellationMode.IMMEDIATE_STOP)
        msgs = graph._state.values["messages"]
        assert _content(msgs[-1]) == "partial reply..."
        assert _content(msgs[0]) == "previous turn"  # prior turn untouched

    @pytest.mark.asyncio
    async def test_no_partial_commit_when_nothing_streamed(self):
        class _HangingGraph(_MockCompiledGraph):
            def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
                async def _gen() -> AsyncIterator[dict[str, Any]]:
                    yield _node_start("answer", "n1")
                    await asyncio.sleep(999)
                    yield _node_end("answer", "n1")  # pragma: no cover

                return _gen()

        graph = _HangingGraph(state=_MockState(values={"messages": []}))
        bridge = LangGraphBridge(graph)
        runner = AgentRunner(bridge, AgentRunnerConfig(timeout=0.05))

        with pytest.raises(AgentTimeoutError):
            async for _ in runner.invoke(AgentTurnInput.from_text("hi"), _recorder()):
                pass

        # Nothing streamed → no empty AI message injected.
        assert graph._state.values["messages"] == []
        assert graph.update_state_calls == []


class TestLangGraphBridgeReducerGuard:
    """``RemoveMessage`` purges and id-keyed rewrites only *merge* into
    an ``add_messages`` reducer channel.  On a plain ``LastValue``
    channel ``update_state`` *replaces* the whole list, so the
    transient-context purge there would wipe the checkpointed
    conversation — the bridge must skip the machinery."""

    def test_messages_key_has_reducer_detection(self):
        graph = _MockCompiledGraph()
        bridge = LangGraphBridge(graph)
        # No introspectable channels → assume reducer (preserve behaviour).
        assert bridge._messages_key_has_reducer() is True

        graph.channels = {"messages": _ReducerChannel()}
        assert bridge._messages_key_has_reducer() is True

        graph.channels = {"messages": LastValue()}
        assert bridge._messages_key_has_reducer() is False

    @pytest.mark.asyncio
    async def test_plain_list_channel_skips_destructive_purge(self):
        graph = _MockCompiledGraph([_node_start("p", "n1"), _node_end("p", "n1")])
        graph.channels = {"messages": LastValue()}
        graph._state.values["messages"] = [_MockMessage("assistant", "kept")]
        bridge = LangGraphBridge(graph)
        turn = AgentTurnInput.from_text(
            "hi", context=[{"role": "system", "content": "Caller id: +1"}]
        )
        async for _ in bridge.invoke(turn, _recorder()):
            pass

        # No RemoveMessage update_state was issued (it would have
        # replaced — wiped — the whole messages list).
        assert graph.update_state_calls == []
        assert [_content(m) for m in graph._state.values["messages"]] == ["kept"]
        # Context was still forwarded for the turn, just untracked.
        assert bridge._transient_context_ids == []

    @pytest.mark.asyncio
    async def test_reducer_channel_still_purges(self):
        graph = _MockCompiledGraph([_node_start("p", "n1"), _node_end("p", "n1")])
        graph.channels = {"messages": _ReducerChannel()}
        bridge = LangGraphBridge(graph)
        turn = AgentTurnInput.from_text(
            "hi", context=[{"role": "system", "content": "Caller id: +1"}]
        )
        async for _ in bridge.invoke(turn, _recorder()):
            pass

        assert graph.update_state_calls
        _cfg, values = graph.update_state_calls[-1]
        # The forwarded context carried a tracked id; the purge issued a
        # removal marker for it.
        assert values["messages"]
        assert all(_id_of(m) for m in values["messages"])


# ── Partial commit on cancel-token break ─────────────────────────


class _CancelAfter:
    """Cancel-token double whose ``is_cancelled`` returns ``False`` for
    the first ``n`` checks, then ``True`` — deterministically simulating
    a barge-in tripped *after* some text has already streamed (the real
    ``CancelToken`` would set its event asynchronously)."""

    def __init__(self, n: int) -> None:
        self._remaining = n

    @property
    def is_cancelled(self) -> bool:
        if self._remaining > 0:
            self._remaining -= 1
            return False
        return True


class TestLangGraphBridgePartialCommitOnCancelToken:
    """A cancel token tripped mid-stream breaks out through the *normal*
    completion path (not the ``BaseException`` cleanup), so the partial
    assistant text must still be committed to the checkpoint there — or a
    follow-up ``apply_interruption()`` rewrites the *previous* turn's AI
    message and corrupts prior LangGraph conversation state."""

    @pytest.mark.asyncio
    async def test_partial_committed_so_interruption_targets_this_turn(self):
        prior_ai = _MockMessage("assistant", "prior reply", message_id="m-prev")
        state = _MockState(values={"messages": [_MockMessage("user", "q1"), prior_ai]})
        scripted = [
            _model_stream("Hello partial", run_id="m"),
            _model_stream(" suppressed", run_id="m"),
        ]
        graph = _MockCompiledGraph(scripted, state=state)
        bridge = LangGraphBridge(graph)

        token = _CancelAfter(1)  # trips on the 2nd loop check
        async for _ in bridge.invoke(
            AgentTurnInput.from_text("q2"), _recorder(), cancel_token=token
        ):
            pass

        # The partial assistant text the caller heard was committed as
        # the new last AI message (not lost on the cancel-token break).
        msgs = graph._state.values["messages"]
        assert _content(msgs[-1]) == "Hello partial"

        # apply_interruption() therefore truncates *this* turn; the
        # previous turn's AI message stays intact.
        bridge.apply_interruption("Hello partial", CancellationMode.IMMEDIATE_STOP)
        assert _content(graph._state.values["messages"][-1]) == "Hello partial..."
        assert _content(prior_ai) == "prior reply"


# ── Resume-thread checkpoint baseline ────────────────────────────


class TestLangGraphBridgeResumeBaseline:
    """Constructing with an explicit ``thread_id`` resumes an existing
    thread whose checkpointer may already hold a long history.  The
    trail baseline must be seeded from the thread's current checkpoint at
    construction so the first turn records only its *own* new checkpoints
    instead of re-walking (and duplicating) the entire persisted
    history."""

    def test_fresh_thread_has_no_seeded_baseline(self):
        graph = _MockCompiledGraph([], state=_MockState(checkpoint_id="cp-1"))
        bridge = LangGraphBridge(graph)
        assert bridge._last_checkpoint_id is None

    def test_resumed_thread_seeds_baseline_at_construction(self):
        graph = _MockCompiledGraph([], state=_MockState(checkpoint_id="cp-prev"))
        bridge = LangGraphBridge(graph, thread_id="existing-thread")
        assert bridge._last_checkpoint_id == "cp-prev"

    def test_resume_seed_failure_degrades_to_none(self):
        class _NoStateGraph(_MockCompiledGraph):
            def get_state(self, config: dict[str, Any]) -> _MockState:
                raise RuntimeError("transient checkpointer error")

        bridge = LangGraphBridge(_NoStateGraph([]), thread_id="existing-thread")
        assert bridge._last_checkpoint_id is None

    @pytest.mark.asyncio
    async def test_seeded_baseline_excludes_preexisting_history(self):
        graph = _MockCompiledGraph(
            [_node_start("p", "n1"), _node_end("p", "n1")],
            state=_MockState(checkpoint_id="cp-prev"),
            state_history=[
                _MockState(checkpoint_id="cp-prev"),
                _MockState(checkpoint_id="cp-old"),
            ],
        )
        bridge = LangGraphBridge(graph, thread_id="existing-thread")
        assert bridge._last_checkpoint_id == "cp-prev"

        # First turn produces cp-new; only it is recorded — cp-prev /
        # cp-old already existed on the resumed thread and must not be
        # re-recorded as if this turn created them.
        graph._state = _MockState(checkpoint_id="cp-new")
        graph.state_history = [
            _MockState(checkpoint_id="cp-new"),
            _MockState(checkpoint_id="cp-prev"),
            _MockState(checkpoint_id="cp-old"),
        ]
        j = InMemoryRingBuffer(capacity=1000)
        async for _ in bridge.invoke(AgentTurnInput.from_text("x"), _recorder(j)):
            pass
        refs = [r.data["state_ref"] for r in j.read() if r.name == "state_snapshot"]
        assert refs == ["langgraph:cp-new"]
