"""LangChain bridge history-store, snapshot, reset, and custom-key tests."""

from __future__ import annotations

from ._langchain_bridge_support import (
    AgentRunner,
    AgentRunnerConfig,
    AgentTimeoutError,
    AgentTurnInput,
    Any,
    AsyncIterator,
    CancellationMode,
    LangChainBridge,
    _content_of_history_item,
    _CopyStoreHistoryRunnable,
    _CustomKeyHistoryRunnable,
    _FakeHistoryRunnable,
    _InMemoryStore,
    _MockAIMessageChunk,
    _MockRunnable,
    _recorder,
    _SingleCustomKeyHistoryRunnable,
    asyncio,
    pytest,
)


class TestLangChainBridgeSnapshot:
    def test_snapshot_state_kind(self):
        runnable = _MockRunnable([])
        bridge = LangChainBridge(runnable, display_name="MyChain")
        snap = bridge.snapshot_state()
        assert snap.kind == "langchain"
        assert snap.fields["runnable"] == "MyChain"
        assert snap.fields["history_length"] == 0


class TestLangChainBridgeHistoryStoreSync:
    async def _bridge_after_turn(self, reply: str = "raw reply") -> tuple[Any, Any]:
        runnable = _FakeHistoryRunnable(reply)
        bridge = LangChainBridge(runnable)
        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            pass
        store = runnable.get_session_history("s1")  # _recorder() → session_id="s1"
        return bridge, store

    @pytest.mark.asyncio
    async def test_markdown_cleanup_mirrored_into_store(self):
        bridge, store = await self._bridge_after_turn()
        bridge.replace_last_assistant_text("cleaned reply")
        assert _content_of_history_item(store.messages[-1]) == "cleaned reply"

    @pytest.mark.asyncio
    async def test_interruption_truncation_mirrored_into_store(self):
        bridge, store = await self._bridge_after_turn()
        bridge.apply_interruption("raw", CancellationMode.IMMEDIATE_STOP)
        assert _content_of_history_item(store.messages[-1]) == "raw..."

    @pytest.mark.asyncio
    async def test_interruption_note_mirrored_into_store(self):
        bridge, store = await self._bridge_after_turn()
        bridge.append_interruption_note("[user interrupted]")
        last = store.messages[-1]
        assert _content_of_history_item(last) == "[user interrupted]"
        role = last.get("role") if isinstance(last, dict) else getattr(last, "type", None)
        assert role in ("system",)

    @pytest.mark.asyncio
    async def test_reset_clears_store(self):
        bridge, store = await self._bridge_after_turn()
        assert store.messages  # populated by the turn
        bridge.reset()
        assert store.messages == []

    @pytest.mark.asyncio
    async def test_plain_runnable_has_no_store_and_is_unaffected(self):
        runnable = _MockRunnable(
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
        bridge = LangChainBridge(runnable)
        async for _ in bridge.invoke(AgentTurnInput.from_text("x"), _recorder()):
            pass
        assert bridge._history_store() is None
        # Post-hoc edits still work on the shadow list.
        bridge.replace_last_assistant_text("clean")
        assert _content_of_history_item(bridge._message_history[-1]) == "clean"

    @pytest.mark.asyncio
    async def test_partial_turn_mirrored_into_store_on_cancel(self):
        """A mid-stream cancel skips the wrapper's end-of-run save
        listener, so the partial turn never lands in the backing store.
        The bridge must mirror it there or a follow-up
        ``apply_interruption()`` (which mirrors its rewrite into the same
        store) would truncate the *previous* turn's assistant message."""

        class _HangingHistoryRunnable:
            def __init__(self) -> None:
                self.history_factory_config: list[Any] = []
                self._stores: dict[str, _InMemoryStore] = {}

            def get_session_history(self, session_id: str) -> _InMemoryStore:
                return self._stores.setdefault(session_id, _InMemoryStore())

            async def astream_events(
                self, input: Any, **kwargs: Any
            ) -> AsyncIterator[dict[str, Any]]:
                yield {
                    "event": "on_chat_model_stream",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": [],
                    "data": {"chunk": _MockAIMessageChunk(content="Hello world")},
                }
                # Run never ends → wrapper's save listener never fires.
                await asyncio.Event().wait()

            async def ainvoke(self, *args: Any, **kwargs: Any) -> Any: ...

        runnable = _HangingHistoryRunnable()
        # A prior, completed turn already persisted in the real store.
        store = runnable.get_session_history("s1")  # _recorder() → session_id="s1"
        store.add_message({"role": "user", "content": "q1"})
        store.add_message({"role": "assistant", "content": "a1"})

        bridge = LangChainBridge(runnable)
        runner = AgentRunner(bridge, AgentRunnerConfig(timeout=0.05))

        with pytest.raises(AgentTimeoutError):
            async for _ in runner.invoke(AgentTurnInput.from_text("q2"), _recorder()):
                pass

        # The partial turn was mirrored into the wrapped store...
        assert _content_of_history_item(store.messages[-2]) == "q2"
        assert _content_of_history_item(store.messages[-1]) == "Hello world"
        # ...so apply_interruption() truncates *this* turn in the store,
        # leaving the prior turn's assistant message intact.
        bridge.apply_interruption("Hello world", CancellationMode.IMMEDIATE_STOP)
        assert _content_of_history_item(store.messages[-1]) == "Hello world..."
        assert _content_of_history_item(store.messages[1]) == "a1"


class TestLangChainBridgeCopyReturningStore:
    """A backing store whose ``.messages`` returns a fetched copy must
    still see markdown-cleanup / interruption rewrites: editing the
    temporary copy in place is lost, so the bridge persists the rewrite
    through the store's own ``clear()`` + ``add_messages()`` API."""

    async def _bridge_after_turn(self, reply: str = "raw reply") -> tuple[Any, Any]:
        runnable = _CopyStoreHistoryRunnable(reply)
        bridge = LangChainBridge(runnable)
        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            pass
        return bridge, runnable.get_session_history("s1")

    @pytest.mark.asyncio
    async def test_markdown_cleanup_persisted_through_backend(self):
        bridge, store = await self._bridge_after_turn()
        bridge.replace_last_assistant_text("cleaned reply")
        # A *fresh* fetch (what the next turn reloads) reflects the edit.
        assert _content_of_history_item(store.messages[-1]) == "cleaned reply"

    @pytest.mark.asyncio
    async def test_interruption_truncation_persisted_through_backend(self):
        bridge, store = await self._bridge_after_turn()
        bridge.apply_interruption("raw", CancellationMode.IMMEDIATE_STOP)
        assert _content_of_history_item(store.messages[-1]) == "raw..."
        # The user turn is untouched and not duplicated by the re-add.
        roles = [
            (m.get("role") if isinstance(m, dict) else getattr(m, "type", None))
            for m in store.messages
        ]
        assert roles == ["user", "assistant"]


class TestLangChainBridgeCustomHistoryFactoryConfig:
    """A custom multi-key ``history_factory_config`` must still resolve
    the real backing store from the configurable, so markdown cleanup /
    interruption truncation / ``reset()`` mutate it (not just the shadow
    list the wrapper ignores on the next turn)."""

    async def _bridge_after_turn(self) -> tuple[Any, Any]:
        runnable = _CustomKeyHistoryRunnable("raw reply")
        bridge = LangChainBridge(
            runnable,
            config={"configurable": {"user_id": "u1", "conversation_id": "c1"}},
        )
        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            pass
        store = runnable.get_session_history(user_id="u1", conversation_id="c1")
        return bridge, store

    @pytest.mark.asyncio
    async def test_store_resolved_from_custom_keys(self):
        bridge, store = await self._bridge_after_turn()
        assert bridge._history_store() is store

    @pytest.mark.asyncio
    async def test_markdown_cleanup_mirrored_into_custom_store(self):
        bridge, store = await self._bridge_after_turn()
        bridge.replace_last_assistant_text("cleaned reply")
        assert _content_of_history_item(store.messages[-1]) == "cleaned reply"

    @pytest.mark.asyncio
    async def test_reset_clears_custom_store(self):
        bridge, store = await self._bridge_after_turn()
        assert store.messages
        bridge.reset()
        assert store.messages == []


class TestLangChainBridgeSingleCustomHistoryKey:
    """LangChain calls ``get_session_history`` *positionally* with a
    single custom ``history_factory_config`` id's value
    (``configurable['conversation_id']``).  The bridge must resolve the
    same store — probing with the synthesized ``session_id`` instead
    targets a different/empty store while the real conversation store
    keeps the raw, untruncated assistant message."""

    async def _bridge_after_turn(self) -> tuple[Any, Any, Any]:
        runnable = _SingleCustomKeyHistoryRunnable("raw reply")
        bridge = LangChainBridge(
            runnable,
            config={"configurable": {"conversation_id": "c1"}},
        )
        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            pass
        store = runnable.get_session_history("c1")
        return bridge, store, runnable

    @pytest.mark.asyncio
    async def test_store_resolved_from_single_custom_key(self):
        bridge, store, runnable = await self._bridge_after_turn()
        assert bridge._history_store() is store
        # No spurious store created under the synthesized session_id.
        assert set(runnable._stores) == {"c1"}

    @pytest.mark.asyncio
    async def test_interruption_truncation_mirrored_into_single_custom_store(self):
        bridge, store, _ = await self._bridge_after_turn()
        bridge.apply_interruption("raw", CancellationMode.IMMEDIATE_STOP)
        assert _content_of_history_item(store.messages[-1]) == "raw..."

    @pytest.mark.asyncio
    async def test_reset_clears_single_custom_store(self):
        bridge, store, _ = await self._bridge_after_turn()
        assert store.messages
        bridge.reset()
        assert store.messages == []


class TestLangChainBridgeResetBeforeFirstTurn:
    """``reset()`` can fire before any turn (a fresh session reused
    immediately).  ``_resolved_session_id`` / ``_resolved_configurable``
    are only populated by the first ``_stream_config``, so the store
    lookup must fall back to the turn-independent constructor args
    (explicit ``session_id=`` / custom ``config=`` keys) — otherwise the
    backing store is left intact and the wrapped runnable reloads stale
    persisted messages on its first invoke."""

    @pytest.mark.asyncio
    async def test_reset_clears_store_with_explicit_session_id(self):
        runnable = _FakeHistoryRunnable("raw reply")
        # An earlier session under this id left persisted history behind.
        store = runnable.get_session_history("sess-A")
        store.add_message({"role": "user", "content": "old q"})
        store.add_message({"role": "assistant", "content": "old a"})

        bridge = LangChainBridge(runnable, session_id="sess-A")
        # No turn has run yet → _resolved_* are still unset.
        assert bridge._resolved_session_id is None
        bridge.reset()
        assert store.messages == []

    @pytest.mark.asyncio
    async def test_reset_clears_store_with_custom_configurable_keys(self):
        runnable = _CustomKeyHistoryRunnable("raw reply")
        store = runnable.get_session_history(user_id="u1", conversation_id="c1")
        store.add_message({"role": "user", "content": "old q"})
        store.add_message({"role": "assistant", "content": "old a"})

        bridge = LangChainBridge(
            runnable,
            config={"configurable": {"user_id": "u1", "conversation_id": "c1"}},
        )
        assert bridge._resolved_configurable is None
        bridge.reset()
        assert store.messages == []

    @pytest.mark.asyncio
    async def test_reset_clears_store_with_single_custom_key(self):
        runnable = _SingleCustomKeyHistoryRunnable("raw reply")
        store = runnable.get_session_history("c1")
        store.add_message({"role": "user", "content": "old q"})
        store.add_message({"role": "assistant", "content": "old a"})

        bridge = LangChainBridge(
            runnable,
            config={"configurable": {"conversation_id": "c1"}},
        )
        bridge.reset()
        assert store.messages == []
        # No spurious store created under a synthesized session_id.
        assert set(runnable._stores) == {"c1"}

    @pytest.mark.asyncio
    async def test_plain_runnable_reset_before_turn_is_safe(self):
        """A plain runnable exposes no store; a pre-turn ``reset()`` must
        still be a no-op (no crash, only the shadow list cleared)."""
        bridge = LangChainBridge(_MockRunnable([]))
        bridge.reset()
        assert bridge._history_store() is None
        assert bridge._message_history == []
