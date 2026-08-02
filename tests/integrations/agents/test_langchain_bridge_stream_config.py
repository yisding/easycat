"""LangChain bridge stream configuration tests."""

from __future__ import annotations

from ._langchain_bridge_support import (
    NULL_RECORDER,
    AgentTurnInput,
    InMemoryRingBuffer,
    JournalAgentRecorder,
    LangChainBridge,
    RecorderContext,
    _MockAIMessageChunk,
    _MockRunnable,
    _recorder,
    pytest,
)


class TestLangChainBridgeStreamConfig:
    """``astream_events`` must carry ``configurable.session_id``.

    ``RunnableWithMessageHistory`` (called out as a supported runnable)
    requires it on *every* invoke/stream — without a config the first
    turn raises ``ValueError: Missing keys ['session_id']`` before any
    event is produced.  Plain runnables ignore the unknown key.
    """

    @staticmethod
    def _runnable() -> _MockRunnable:
        return _MockRunnable(
            [
                {
                    "event": "on_chat_model_stream",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": [],
                    "data": {"chunk": _MockAIMessageChunk(content="hi")},
                }
            ]
        )

    @pytest.mark.asyncio
    async def test_default_threads_recorder_session_id(self):
        runnable = self._runnable()
        bridge = LangChainBridge(runnable)
        async for _ in bridge.invoke(AgentTurnInput.from_text("x"), _recorder()):
            pass
        config = runnable.invoked_with[1]["config"]
        # ``_recorder()`` builds RecorderContext(session_id="s1").
        assert config["configurable"]["session_id"] == "s1"

    @pytest.mark.asyncio
    async def test_explicit_session_id_overrides_recorder(self):
        runnable = self._runnable()
        bridge = LangChainBridge(runnable, session_id="explicit-sid")
        async for _ in bridge.invoke(AgentTurnInput.from_text("x"), _recorder()):
            pass
        config = runnable.invoked_with[1]["config"]
        assert config["configurable"]["session_id"] == "explicit-sid"

    @pytest.mark.asyncio
    async def test_base_config_keys_preserved_and_session_id_not_clobbered(self):
        """Caller ``config=`` is the merge base; a caller-supplied
        ``configurable.session_id`` (custom ``history_factory_config``)
        is preserved, other keys pass through untouched."""
        runnable = self._runnable()
        bridge = LangChainBridge(
            runnable,
            config={
                "configurable": {"session_id": "caller-sid", "user_id": "u1"},
                "tags": ["voice"],
            },
        )
        async for _ in bridge.invoke(AgentTurnInput.from_text("x"), _recorder()):
            pass
        config = runnable.invoked_with[1]["config"]
        assert config["configurable"]["session_id"] == "caller-sid"
        assert config["configurable"]["user_id"] == "u1"
        assert config["tags"] == ["voice"]

    @pytest.mark.asyncio
    async def test_malformed_configurable_records_error_exit_and_closes_cursor(self):
        runnable = self._runnable()
        bridge = LangChainBridge(
            runnable,
            config={"configurable": 1},  # type: ignore[dict-item]
        )
        journal = InMemoryRingBuffer(capacity=1000)
        recorder = _recorder(journal)

        with pytest.raises(TypeError):
            async for _ in bridge.invoke(AgentTurnInput.from_text("x"), recorder):
                pass

        records = journal.read()
        assert [record.name for record in records] == [
            "unit_entered",
            "framework_error",
            "unit_exited",
        ]
        assert records[1].error is not None
        assert records[1].error.type == "TypeError"
        assert records[2].data["exit_reason"] == "error"
        assert recorder._open_cursors == []

    @pytest.mark.asyncio
    async def test_fallback_session_id_stable_across_turns_without_journal(self):
        """Driven via NULL_RECORDER (session_id="") the bridge must still
        thread a *stable* id so a wrapped history runnable accumulates
        correctly across turns."""
        runnable = self._runnable()
        bridge = LangChainBridge(runnable)
        async for _ in bridge.invoke(AgentTurnInput.from_text("a"), NULL_RECORDER):
            pass
        first = runnable.invoked_with[1]["config"]["configurable"]["session_id"]
        async for _ in bridge.invoke(AgentTurnInput.from_text("b"), NULL_RECORDER):
            pass
        second = runnable.invoked_with[1]["config"]["configurable"]["session_id"]
        assert first and first == second

    @pytest.mark.asyncio
    async def test_rotating_run_id_does_not_rekey_history(self):
        """``AgentStage`` mints a fresh ``run-<hex>`` every turn and, used
        directly, leaves ``session_id`` empty.  The resolved id must stay
        the stable per-bridge fallback across turns — not the rotating
        ``run_id`` — or a wrapped ``RunnableWithMessageHistory`` would be
        re-keyed each turn and drop prior conversation."""
        runnable = self._runnable()
        bridge = LangChainBridge(runnable)

        def _rec(run_id: str) -> JournalAgentRecorder:
            return JournalAgentRecorder(
                journal=InMemoryRingBuffer(capacity=1000),
                artifact_store=None,
                context=RecorderContext(run_id=run_id, session_id=""),
            )

        async for _ in bridge.invoke(AgentTurnInput.from_text("a"), _rec("run-a")):
            pass
        first = runnable.invoked_with[1]["config"]["configurable"]["session_id"]
        async for _ in bridge.invoke(AgentTurnInput.from_text("b"), _rec("run-b")):
            pass
        second = runnable.invoked_with[1]["config"]["configurable"]["session_id"]
        assert first == second == bridge._fallback_session_id
        assert first not in ("run-a", "run-b")

    @pytest.mark.asyncio
    async def test_independent_bridges_do_not_collide_without_session_id(self):
        """Two independent bridges driven via ``NULL_RECORDER`` (shared
        literal ``run_id="null"``) must resolve *distinct* ids so their
        wrapped history stores can't cross-contaminate."""
        bridge_a = LangChainBridge(self._runnable())
        bridge_b = LangChainBridge(self._runnable())
        async for _ in bridge_a.invoke(AgentTurnInput.from_text("a"), NULL_RECORDER):
            pass
        async for _ in bridge_b.invoke(AgentTurnInput.from_text("b"), NULL_RECORDER):
            pass
        sid_a = bridge_a._resolved_session_id
        sid_b = bridge_b._resolved_session_id
        assert sid_a and sid_b and sid_a != sid_b
        assert "null" not in (sid_a, sid_b)
