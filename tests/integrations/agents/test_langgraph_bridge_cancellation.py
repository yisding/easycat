"""LangGraph bridge cancellation and partial-commit tests."""

from __future__ import annotations

from ._langgraph_bridge_support import (
    AgentRunner,
    AgentRunnerConfig,
    AgentTimeoutError,
    AgentTurnInput,
    Any,
    AsyncIterator,
    CancellationMode,
    LangGraphBridge,
    _CancelAfter,
    _content,
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
                    await asyncio.Event().wait()
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
                    await asyncio.Event().wait()
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

    @pytest.mark.asyncio
    async def test_early_cancel_does_not_rewrite_prior_turn(self):
        """Cancelled before the first token with a prior turn already in
        the checkpoint: nothing is committed for this turn, so a
        follow-up ``apply_interruption("")`` must no-op rather than walk
        back and truncate the *previous* turn's AI message."""

        class _HangingGraph(_MockCompiledGraph):
            def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
                async def _gen() -> AsyncIterator[dict[str, Any]]:
                    yield _node_start("answer", "n1")
                    await asyncio.Event().wait()
                    yield _node_end("answer", "n1")  # pragma: no cover

                return _gen()

        prior_ai = _MockMessage("assistant", "previous turn", message_id="prev")
        graph = _HangingGraph(state=_MockState(values={"messages": [prior_ai]}))
        bridge = LangGraphBridge(graph)
        runner = AgentRunner(bridge, AgentRunnerConfig(timeout=0.05))

        with pytest.raises(AgentTimeoutError):
            async for _ in runner.invoke(AgentTurnInput.from_text("hi"), _recorder()):
                pass

        bridge.apply_interruption("", CancellationMode.IMMEDIATE_STOP)

        # The prior turn's reply is untouched and no rewrite was issued.
        assert _content(graph._state.values["messages"][-1]) == "previous turn"
        assert graph.update_state_calls == []


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


class TestLangGraphBridgeNoAiMessageThisTurn:
    """A *successful* turn can leave the checkpoint ending at the user's
    message — a router branch that only narrates via
    ``get_stream_writer`` or returns ``{}`` appends no ``AIMessage``.
    The cancelled-turn flag does not cover this, so the history rewrite
    must bound its backward scan at the latest user turn and no-op
    rather than reach back and corrupt the *previous* turn's reply."""

    def _custom_only_turn_state(self) -> tuple[_MockCompiledGraph, _MockMessage]:
        prior_ai = _MockMessage("assistant", "prior reply", message_id="m-prev")
        # What the checkpoint holds after a custom-only turn: the new
        # user message is appended but no AI message follows it.
        state = _MockState(
            values={
                "messages": [
                    _MockMessage("user", "q1"),
                    prior_ai,
                    _MockMessage("user", "q2"),
                ]
            }
        )
        custom_chunk = {
            "event": "on_chain_stream",
            "name": "LangGraph",
            "run_id": "g1",
            "data": {"chunk": ("custom", {"text": "**progress**"})},
            "metadata": {},
        }
        scripted = [
            _node_start("route", "n1"),
            custom_chunk,
            _node_end("route", "n1"),
        ]
        return _MockCompiledGraph(scripted, state=state), prior_ai

    @pytest.mark.asyncio
    async def test_replace_last_assistant_text_does_not_touch_prior_turn(self):
        graph, prior_ai = self._custom_only_turn_state()
        bridge = LangGraphBridge(graph)

        events = [ev async for ev in bridge.invoke(AgentTurnInput.from_text("q2"), _recorder())]
        assert [e.text for e in events if e.kind == "text_delta"] == ["**progress**"]

        bridge.replace_last_assistant_text("progress")

        # Prior turn's reply is untouched and no rewrite was issued.
        assert _content(prior_ai) == "prior reply"
        assert graph.update_state_calls == []

    @pytest.mark.asyncio
    async def test_apply_interruption_does_not_touch_prior_turn(self):
        graph, prior_ai = self._custom_only_turn_state()
        bridge = LangGraphBridge(graph)

        async for _ in bridge.invoke(AgentTurnInput.from_text("q2"), _recorder()):
            pass

        bridge.apply_interruption("**progress**", CancellationMode.IMMEDIATE_STOP)

        assert _content(prior_ai) == "prior reply"
        assert graph.update_state_calls == []
