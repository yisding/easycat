"""LangGraph bridge state tests."""

from __future__ import annotations

from ._langgraph_bridge_support import (
    AgentTurnInput,
    Any,
    AsyncIterator,
    BridgeInputError,
    CancellationMode,
    InMemoryRingBuffer,
    LangGraphBridge,
    _MockAIMessageChunk,
    _MockCompiledGraph,
    _MockMessage,
    _MockState,
    _model_stream,
    _node_end,
    _node_start,
    _recorder,
    pytest,
)


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
        with a ``text`` field should drive TTS; opaque telemetry and
        bare debug/progress strings should stay silent."""
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
        assert text_deltas == ["Looking that up..."]

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
            # LangGraph special-casing this becomes the dedup's root run id.
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
    async def test_nested_lcel_node_intermediate_not_spoken(self):
        """A node that is itself an LCEL sequence
        (``RunnableLambda(f) | RunnableLambda(g)``) emits a child
        ``on_chain_stream`` for ``f``'s intermediate value and a
        node-entry ``on_chain_stream`` for the composed result.  Only
        the node entry is root-equivalent, so the intermediate must be
        deduped — otherwise the caller hears the intermediate rather
        than the final node response."""
        scripted = [
            {  # graph root (no parent) → LCEL root run id
                "event": "on_chain_start",
                "name": "LangGraph",
                "run_id": "graph",
                "parent_ids": [],
                "data": {},
                "metadata": {},
            },
            {  # node entry: name == langgraph_node → node root
                "event": "on_chain_start",
                "name": "answer",
                "run_id": "n1",
                "parent_ids": ["graph"],
                "data": {},
                "metadata": {"langgraph_node": "answer", "langgraph_step": 1},
            },
            {  # inner child runnable ``f`` (not the node entry)
                "event": "on_chain_start",
                "name": "f",
                "run_id": "c1",
                "parent_ids": ["graph", "n1"],
                "data": {},
                "metadata": {"langgraph_node": "answer"},
            },
            {  # f's intermediate value — must be deduped
                "event": "on_chain_stream",
                "name": "f",
                "run_id": "c1",
                "parent_ids": ["graph", "n1"],
                "data": {"chunk": "INTERMEDIATE"},
                "metadata": {"langgraph_node": "answer"},
            },
            {  # node-entry composed output — forwarded
                "event": "on_chain_stream",
                "name": "answer",
                "run_id": "n1",
                "parent_ids": ["graph"],
                "data": {"chunk": "the real answer"},
                "metadata": {"langgraph_node": "answer"},
            },
            {
                "event": "on_chain_end",
                "name": "answer",
                "run_id": "n1",
                "parent_ids": ["graph"],
                "data": {},
                "metadata": {"langgraph_node": "answer"},
            },
        ]
        graph = _MockCompiledGraph(scripted)
        bridge = LangGraphBridge(graph)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("x"), _recorder()):
            events.append(ev)

        text_deltas = [e.text for e in events if e.kind == "text_delta"]
        assert text_deltas == ["the real answer"]
        assert "INTERMEDIATE" not in "".join(text_deltas)

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
        ``on_llm_*`` events.  The default (no ``include_types`` filter)
        must surface those so the answer isn't filtered out before
        translation — otherwise the turn ends silent with an empty
        ``done.text``.  A narrow tuple must not be silently re-added:
        LangChain keys ``on_custom_event`` on the event name, so any
        ``include_types`` would also drop the custom-event TTS path.

        The realistic ``on_llm_start`` event precedes the stream here —
        omitting it would mask the parented-LLM-redaction regression,
        since the redaction set is only populated on ``on_llm_start``.
        A LangGraph node that directly invokes a ``BaseLLM`` parents the
        run on the node root, whose own ``on_chain_stream`` carries a
        state dict that ``_dict_output_text`` filters out, so suppressing
        the raw ``on_llm_*`` tokens here would leave the node silent."""

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
                "event": "on_llm_start",
                "name": "OpenAI",
                "run_id": "l1",
                "parent_ids": ["n1"],
                "data": {},
                "metadata": {"langgraph_node": "answer", "checkpoint_id": "cp-1"},
            },
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

        assert "include_types" not in captured["kwargs"]
        text = "".join(e.text for e in events if e.kind == "text_delta")
        assert text == "completion text"
        done = [e for e in events if e.kind == "done"]
        assert done and done[0].text == "completion text"

    @pytest.mark.asyncio
    async def test_node_direct_non_chat_llm_to_plain_state_field_is_audible(self):
        """Scenario B: a node directly invokes a non-chat ``BaseLLM`` and
        writes its output to a plain (non-``messages``) state field.  The
        node's own ``on_chain_stream`` carries a state dict that
        ``_dict_output_text`` filters out, and the final messages tail is
        not an AI message — so the only audible text source is the raw
        ``on_llm_*`` stream.  It must NOT be redacted as a parented LCEL
        run, or the turn ends silent.  Asserts a non-empty ``done.text``
        end to end (driven by the streamed LLM tokens, since the messages
        tail stays the user's own turn)."""

        class _GenerationChunk:
            def __init__(self, text: str) -> None:
                self.text = text

        user_msg = _MockMessage("user", "hi")
        # Output landed in a plain "draft" field, not "messages" — the
        # messages tail stays the user turn so ``_last_output`` is not an
        # AI message and ``done.text`` falls back to the streamed text.
        state = _MockState(
            values={"messages": [user_msg], "draft": "completion text"},
            checkpoint_id="cp-final",
        )
        scripted = [
            _node_start("answer", "n1"),
            {
                "event": "on_llm_start",
                "name": "OpenAI",
                "run_id": "l1",
                "parent_ids": ["n1"],
                "data": {},
                "metadata": {"langgraph_node": "answer", "checkpoint_id": "cp-1"},
            },
            {
                "event": "on_llm_stream",
                "name": "OpenAI",
                "run_id": "l1",
                "parent_ids": ["n1"],
                "data": {"chunk": _GenerationChunk("completion text")},
                "metadata": {"langgraph_node": "answer", "checkpoint_id": "cp-1"},
            },
            # The node's composed state output — a dict that
            # ``_dict_output_text`` filters out (no conventional key).
            {
                "event": "on_chain_stream",
                "name": "answer",
                "run_id": "n1",
                "parent_ids": [],
                "data": {"chunk": {"draft": "completion text"}},
                "metadata": {"langgraph_node": "answer", "checkpoint_id": "cp-1"},
            },
            _node_end("answer", "n1"),
        ]
        graph = _MockCompiledGraph(scripted, state=state)
        bridge = LangGraphBridge(graph)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)

        text = "".join(e.text for e in events if e.kind == "text_delta")
        done = [e for e in events if e.kind == "done"]
        assert text == "completion text"
        assert done and done[0].text == "completion text"

    @pytest.mark.asyncio
    async def test_nested_lcel_llm_under_node_uses_redacted_node_stream(self):
        """A BaseLLM nested inside an LCEL chain under a LangGraph node must
        not be treated as node-direct just because the node run id appears
        somewhere in LangChain v2's full ancestor chain.  Its raw tokens
        stay suppressed so the node's composed/redacted output is the only
        public text."""

        class _GenerationChunk:
            def __init__(self, text: str) -> None:
                self.text = text

        ai_msg = _MockMessage("assistant", "[REDACTED]", message_id="m-1")
        state = _MockState(
            values={"messages": [_MockMessage("user", "hi"), ai_msg]},
            checkpoint_id="cp-final",
        )
        scripted = [
            _node_start("answer", "n1"),
            {
                "event": "on_chain_start",
                "name": "RunnableSequence",
                "run_id": "seq",
                "parent_ids": ["n1"],
                "data": {},
                "metadata": {"langgraph_node": "answer", "checkpoint_id": "cp-1"},
            },
            {
                "event": "on_llm_start",
                "name": "FakeStreamingListLLM",
                "run_id": "l1",
                "parent_ids": ["n1", "seq"],
                "data": {},
                "metadata": {"langgraph_node": "answer", "checkpoint_id": "cp-1"},
            },
            {
                "event": "on_llm_stream",
                "name": "FakeStreamingListLLM",
                "run_id": "l1",
                "parent_ids": ["n1", "seq"],
                "data": {"chunk": _GenerationChunk("SECRET_TOKEN=abc123")},
                "metadata": {"langgraph_node": "answer", "checkpoint_id": "cp-1"},
            },
            {
                "event": "on_chain_stream",
                "name": "answer",
                "run_id": "n1",
                "parent_ids": [],
                "data": {"chunk": {"output": "[REDACTED]"}},
                "metadata": {"langgraph_node": "answer", "checkpoint_id": "cp-1"},
            },
            _node_end("answer", "n1"),
        ]
        graph = _MockCompiledGraph(scripted, state=state)
        bridge = LangGraphBridge(graph)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)

        text = "".join(e.text for e in events if e.kind == "text_delta")
        done = [e for e in events if e.kind == "done"]
        assert text == "[REDACTED]"
        assert "SECRET_TOKEN" not in text
        assert done and done[0].text == "[REDACTED]"

    @pytest.mark.asyncio
    async def test_suppressed_parented_llm_without_public_output_stays_silent(self):
        """Suppressed parented non-chat LLM output must not become a raw
        fallback when the surrounding chain/node emits no public text.

        Downstream LCEL components may redact, select, or intentionally
        drop the raw model output. If the final messages tail is not an
        AI message and no composed public text streams, the bridge should
        finish silently rather than speaking the suppressed raw result.
        """

        user_msg = _MockMessage("user", "hi")
        state = _MockState(
            values={"messages": [user_msg], "internal": "dropped"},
            checkpoint_id="cp-final",
        )
        scripted = [
            _node_start("answer", "n1"),
            {
                "event": "on_chain_start",
                "name": "RunnableSequence",
                "run_id": "seq",
                "parent_ids": ["n1"],
                "data": {},
                "metadata": {"langgraph_node": "answer", "checkpoint_id": "cp-1"},
            },
            {
                "event": "on_llm_start",
                "name": "OpenAI",
                "run_id": "l1",
                "parent_ids": ["n1", "seq"],
                "data": {},
                "metadata": {"langgraph_node": "answer", "checkpoint_id": "cp-1"},
            },
            {
                "event": "on_llm_end",
                "name": "OpenAI",
                "run_id": "l1",
                "parent_ids": ["n1", "seq"],
                "data": {"output": {"generations": [[{"text": "SECRET_TOKEN=abc123"}]]}},
                "metadata": {"langgraph_node": "answer", "checkpoint_id": "cp-1"},
            },
            # The node's composed state output is intentionally not
            # speakable (no conventional public text key).
            {
                "event": "on_chain_stream",
                "name": "answer",
                "run_id": "n1",
                "parent_ids": [],
                "data": {"chunk": {"internal": "dropped"}},
                "metadata": {"langgraph_node": "answer", "checkpoint_id": "cp-1"},
            },
            _node_end("answer", "n1"),
        ]
        graph = _MockCompiledGraph(scripted, state=state)
        bridge = LangGraphBridge(graph)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)

        text = "".join(e.text for e in events if e.kind == "text_delta")
        done = [e for e in events if e.kind == "done"]
        assert text == ""
        assert done and done[0].text == ""
        assert all("SECRET_TOKEN" not in (e.text or "") for e in events)

    @pytest.mark.asyncio
    async def test_dispatch_custom_event_drives_text_delta_by_default(self):
        """A graph node using LangChain's ``dispatch_custom_event`` emits
        ``on_custom_event`` through ``astream_events``.  LangChain keys
        that event on its *name* (not a runnable type), so a non-``None``
        ``include_types`` would silently drop it.  Under the default
        (unfiltered) the speakable payload must reach the translator."""

        captured: dict[str, Any] = {}

        class _CapturingGraph(_MockCompiledGraph):
            def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
                captured["kwargs"] = kwargs
                return super().astream_events(input, **kwargs)

        scripted = [
            _node_start("answer", "n1"),
            {
                "event": "on_custom_event",
                "name": "status",
                "run_id": "c1",
                "parent_ids": ["n1"],
                "data": {"text": "thinking..."},
                "metadata": {"langgraph_node": "answer", "checkpoint_id": "cp-1"},
            },
            _node_end("answer", "n1"),
        ]
        graph = _CapturingGraph(scripted)
        bridge = LangGraphBridge(graph)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)

        # The bridge must not silently re-add a filter that would strip
        # the event upstream before the translator sees it.
        assert "include_types" not in captured["kwargs"]
        text_events = [e for e in events if e.kind == "text_delta"]
        assert text_events and text_events[0].text == "thinking..."

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
