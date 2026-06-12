"""LangGraph bridge resume, bound-thread, checkpoint, and baseline tests."""

from __future__ import annotations

from ._langgraph_bridge_support import (
    AgentTurnInput,
    Any,
    CancellationMode,
    InMemoryRingBuffer,
    LangGraphBridge,
    _BoundConfigGraph,
    _ConfigRecordingGraph,
    _MockCompiledGraph,
    _MockMessage,
    _MockState,
    _model_stream,
    _node_end,
    _node_start,
    _recorder,
    pytest,
)


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


class TestLangGraphBridgeBoundThreadId:
    """A caller resuming via ``graph.with_config(configurable=...)`` is
    the only way ``auto_adapt_agent`` can carry a resume thread through.
    The bridge must honour that bound id instead of minting a fresh UUID
    (which would write to an empty checkpoint and lose the history)."""

    def test_bound_thread_id_is_honoured(self):
        graph = _BoundConfigGraph("resume-thread")
        bridge = LangGraphBridge(graph=graph)
        assert bridge._thread_id == "resume-thread"

    def test_explicit_thread_id_wins_over_bound(self):
        graph = _BoundConfigGraph("bound-thread")
        bridge = LangGraphBridge(graph=graph, thread_id="explicit-thread")
        assert bridge._thread_id == "explicit-thread"

    def test_fresh_graph_still_mints_uuid(self):
        bridge = LangGraphBridge(graph=_MockCompiledGraph([]))
        assert bridge._thread_id and bridge._thread_id != "resume-thread"

    def test_bound_thread_seeds_resume_baseline(self):
        # A bound thread id is a resume just like an explicit one: its
        # prior-history checkpoint baseline must be seeded too, so the
        # first turn doesn't re-walk the whole persisted history.
        graph = _BoundConfigGraph("resume-thread", state=_MockState(checkpoint_id="cp-prev"))
        bridge = LangGraphBridge(graph=graph)
        assert bridge._last_checkpoint_id == "cp-prev"


class TestLangGraphBridgeBoundCheckpointId:
    """A caller may bind ``configurable.checkpoint_id`` (a LangGraph
    resume/time-travel config: "run from this checkpoint").  LangGraph
    treats a pinned ``checkpoint_id`` as "fork from here", so reusing it
    every turn keeps forking the original snapshot and ``get_state``
    reads stale state — losing all conversation progress after the first
    resumed turn.  It must be a one-shot cursor: the construction
    baseline seed + first turn's stream, then dropped."""

    def test_resume_cursor_captured_at_construction(self):
        graph = _ConfigRecordingGraph("t-resume", "cp-pinned")
        bridge = LangGraphBridge(graph=graph)
        assert bridge._thread_id == "t-resume"
        assert bridge._resume_checkpoint_id == "cp-pinned"
        # Baseline seed read the pinned checkpoint (so a time-travel
        # resume doesn't re-walk the forked-from history).
        assert graph.get_state_cps == ["cp-pinned"]

    def test_config_does_not_pin_bound_checkpoint(self):
        # _config() is the current-state config: it must neutralise the
        # bound checkpoint_id (explicit None == latest) so post-turn
        # reads see the latest checkpoint, not the pinned snapshot.
        bridge = LangGraphBridge(graph=_ConfigRecordingGraph("t", "cp-pinned"))
        assert bridge._config()["configurable"]["checkpoint_id"] is None

    @pytest.mark.asyncio
    async def test_resume_cursor_is_one_shot_across_turns(self):
        graph = _ConfigRecordingGraph(
            "t-resume", "cp-pinned", state=_MockState(checkpoint_id="cp-new")
        )
        bridge = LangGraphBridge(graph=graph)
        graph.get_state_cps.clear()  # drop the construction-seed read

        async for _ in bridge.invoke(AgentTurnInput.from_text("one"), _recorder()):
            pass
        # First turn forks from the pinned checkpoint…
        assert graph.astream_cps == ["cp-pinned"]
        # …but its post-stream get_state reads the latest (not the pin),
        # and the cursor is consumed.
        assert graph.get_state_cps and all(cp is None for cp in graph.get_state_cps)
        assert bridge._resume_checkpoint_id is None

        graph.astream_cps.clear()
        graph.get_state_cps.clear()
        async for _ in bridge.invoke(AgentTurnInput.from_text("two"), _recorder()):
            pass
        # Second turn must NOT re-fork the original snapshot.
        assert graph.astream_cps == [None]
        assert all(cp is None for cp in graph.get_state_cps)

    def test_reset_clears_resume_cursor(self):
        bridge = LangGraphBridge(graph=_ConfigRecordingGraph("t", "cp-pinned"))
        bridge.reset()
        assert bridge._resume_checkpoint_id is None


class TestLangGraphBridgeBaselineAfterStateWrite:
    """``replace_last_assistant_text`` / ``apply_interruption`` /
    ``append_interruption_note`` call ``update_state`` *between* turns,
    creating a fresh checkpoint.  The trail baseline must advance to it
    so the next turn's checkpoint trail doesn't re-record that
    rewrite/interruption checkpoint as a ``state_snapshot`` belonging to
    the *following* user turn."""

    def _turn_one_graph(self) -> _MockCompiledGraph:
        state = _MockState(
            values={
                "messages": [
                    _MockMessage("user", "q1"),
                    _MockMessage("assistant", "raw **md**", message_id="m1"),
                ]
            },
            checkpoint_id="cp-1",
        )
        scripted = [
            _node_start("agent", "n1"),
            _model_stream("raw md"),
            _node_end("agent", "n1"),
        ]
        return _MockCompiledGraph(scripted, state=state)

    @pytest.mark.asyncio
    async def test_replace_last_assistant_text_advances_baseline(self):
        graph = self._turn_one_graph()
        bridge = LangGraphBridge(graph)

        async for _ in bridge.invoke(AgentTurnInput.from_text("q1"), _recorder()):
            pass
        assert bridge._last_checkpoint_id == "cp-1"

        # Markdown cleanup writes a new checkpoint between turns; the
        # baseline must move to it (``update_state`` → cp-2).
        bridge.replace_last_assistant_text("raw md")
        assert graph.update_state_calls  # the rewrite actually fired
        assert bridge._last_checkpoint_id == "cp-2"

        # Turn 2: history grew to [cp-3, cp-2, cp-1] (newest→oldest).
        # With the advanced baseline the walk stops at cp-2, so only
        # this turn's cp-3 is recorded — the rewrite's cp-2 is *not*
        # misattributed to turn 2.
        graph._state = _MockState(checkpoint_id="cp-3")
        graph.state_history = [
            _MockState(checkpoint_id="cp-3"),
            _MockState(checkpoint_id="cp-2"),
            _MockState(checkpoint_id="cp-1"),
        ]
        j2 = InMemoryRingBuffer(capacity=1000)
        async for _ in bridge.invoke(AgentTurnInput.from_text("q2"), _recorder(j2)):
            pass
        refs2 = [r.data["state_ref"] for r in j2.read() if r.name == "state_snapshot"]
        assert refs2 == ["langgraph:cp-3"]

    @pytest.mark.asyncio
    async def test_apply_interruption_advances_baseline(self):
        graph = self._turn_one_graph()
        bridge = LangGraphBridge(graph)

        async for _ in bridge.invoke(AgentTurnInput.from_text("q1"), _recorder()):
            pass
        assert bridge._last_checkpoint_id == "cp-1"

        bridge.apply_interruption("raw", CancellationMode.IMMEDIATE_STOP)
        assert graph.update_state_calls
        assert bridge._last_checkpoint_id == "cp-2"

    @pytest.mark.asyncio
    async def test_append_interruption_note_advances_baseline(self):
        graph = self._turn_one_graph()
        bridge = LangGraphBridge(graph)

        async for _ in bridge.invoke(AgentTurnInput.from_text("q1"), _recorder()):
            pass
        assert bridge._last_checkpoint_id == "cp-1"

        bridge.append_interruption_note("[user interrupted]")
        assert graph.update_state_calls
        assert bridge._last_checkpoint_id == "cp-2"
