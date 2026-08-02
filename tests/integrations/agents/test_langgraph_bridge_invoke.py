"""LangGraph bridge construction and invoke tests."""

from __future__ import annotations

from ._langgraph_bridge_support import (
    AgentRunner,
    AgentRunnerConfig,
    AgentTimeoutError,
    AgentTurnInput,
    Any,
    AsyncIterator,
    BridgeInputError,
    CancelToken,
    CommitRule,
    InMemoryRingBuffer,
    LangGraphBridge,
    UnitKind,
    _MockAIMessageChunk,
    _MockCompiledGraph,
    _MockMessage,
    _MockState,
    _model_stream,
    _node_end,
    _node_start,
    _recorder,
    asyncio,
    pytest,
)


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

    def test_rejects_graph_with_true_checkpointer(self):
        """``graph.compile(checkpointer=True)`` is the inherit-from-parent
        sentinel: ``graph.checkpointer`` is the literal ``True`` (no real
        checkpointer).  ``not True`` is ``False`` so a naive falsy check
        accepts it, but the first ``invoke()`` raises ``RuntimeError:
        checkpointer=True cannot be used for root graphs``.  The bridge
        must reject it at construction with its actionable error."""

        class GraphTrueCP:
            checkpointer = True

            def astream_events(self, *args: Any, **kwargs: Any) -> Any:
                return iter(())

        with pytest.raises(BridgeInputError, match="checkpointer"):
            LangGraphBridge(GraphTrueCP())

    def test_committable_boundaries_published(self):
        assert LangGraphBridge.COMMITTABLE_BOUNDARIES[UnitKind.WORKFLOW_NODE] == (
            CommitRule.BETWEEN_NODES
        )
        assert LangGraphBridge.COMMITTABLE_BOUNDARIES[UnitKind.AGENT] == (CommitRule.BETWEEN_TURNS)


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
    async def test_ignores_non_mapping_stream_events(self):
        """A malformed provider event must not abort a later valid response."""
        graph = _MockCompiledGraph(
            [
                ["not an event object"],
                _model_stream("still works"),
            ]
        )
        bridge = LangGraphBridge(graph)

        events = []
        async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(event)

        text = "".join(event.text for event in events if event.kind == "text_delta")
        assert text == "still works"
        assert [event.kind for event in events][-1] == "done"

    @pytest.mark.asyncio
    async def test_ignores_stream_events_with_invalid_nested_fields(self):
        graph = _MockCompiledGraph(
            [
                {
                    "event": "on_chat_model_start",
                    "name": "ChatOpenAI",
                    "run_id": "bad-parents",
                    "parent_ids": 1,
                    "data": {},
                    "metadata": {},
                },
                {
                    "event": "on_chain_start",
                    "name": "research",
                    "run_id": "bad-namespace",
                    "parent_ids": [],
                    "data": {},
                    "metadata": {
                        "langgraph_node": "research",
                        "langgraph_checkpoint_ns": [],
                    },
                },
                _model_stream("still works"),
            ]
        )
        bridge = LangGraphBridge(graph)

        events = []
        async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(event)

        text = "".join(event.text for event in events if event.kind == "text_delta")
        assert text == "still works"
        assert [event.kind for event in events][-1] == "done"

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
                    await asyncio.Event().wait()
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
        # Handoffs live solely in the journal; the stream carries no handoff
        # events.
        assert not any(e.kind == "handoff" for e in events)

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
