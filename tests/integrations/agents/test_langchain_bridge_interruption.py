"""LangChain bridge interruption and cancellation tests."""

from __future__ import annotations

from ._langchain_bridge_support import (
    AgentRunner,
    AgentRunnerConfig,
    AgentTimeoutError,
    AgentTurnInput,
    Any,
    AsyncIterator,
    CancellationMode,
    InMemoryRingBuffer,
    LangChainBridge,
    _content_of_history_item,
    _InMemoryStore,
    _MockAIMessageChunk,
    _MockRunnable,
    _recorder,
    _role_of_msg,
    asyncio,
    pytest,
)


class TestLangChainBridgeInterruption:
    def test_apply_interruption_rewrites_last_ai(self):
        runnable = _MockRunnable([])
        bridge = LangChainBridge(runnable)
        bridge._message_history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "the full reply"},
        ]
        bridge.apply_interruption("the full", CancellationMode.IMMEDIATE_STOP)
        assert bridge._message_history[-1]["content"] == "the full..."

    def test_apply_interruption_with_journal(self):
        runnable = _MockRunnable([])
        bridge = LangChainBridge(runnable)
        bridge._message_history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "long answer"},
        ]
        journal = InMemoryRingBuffer(capacity=100)
        rec = _recorder(journal)
        bridge.apply_interruption("long", CancellationMode.IMMEDIATE_STOP, recorder=rec)
        names = [r.name for r in journal.read()]
        assert "state_committed" in names
        assert "cancellation_boundary" in names

    def test_reset_clears_history(self):
        runnable = _MockRunnable([])
        bridge = LangChainBridge(runnable)
        bridge._message_history.append({"role": "user", "content": "x"})
        bridge.reset()
        assert bridge._message_history == []


class TestLangChainBridgePartialTurnOnCancel:
    """A turn cancelled mid-stream (AgentRunner timeout / barge-in
    ``aclose()``) lands in the ``BaseException`` cleanup path, which is
    skipped by the normal history-recording code below it.  The bridge
    must persist the partial turn there so a follow-up
    ``apply_interruption()`` truncates *this* turn — not the previous
    turn's assistant message (or a no-op on turn one)."""

    @pytest.mark.asyncio
    async def test_partial_preserved_and_interruption_truncates_this_turn(self):
        class _HangingRunnable:
            async def astream_events(
                self, input: Any, **kwargs: Any
            ) -> AsyncIterator[dict[str, Any]]:
                yield {
                    "event": "on_chat_model_start",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": [],
                    "data": {},
                }
                yield {
                    "event": "on_chat_model_stream",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": [],
                    "data": {"chunk": _MockAIMessageChunk(content="Hello world")},
                }
                await asyncio.Event().wait()

            async def ainvoke(self, *args: Any, **kwargs: Any) -> Any: ...

        bridge = LangChainBridge(_HangingRunnable())
        # A prior, completed turn already in history.
        bridge._message_history = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]
        runner = AgentRunner(bridge, AgentRunnerConfig(timeout=0.05))

        with pytest.raises(AgentTimeoutError):
            async for _ in runner.invoke(AgentTurnInput.from_text("q2"), _recorder()):
                pass

        # The partial turn was recorded (not lost on cancel).
        assert _content_of_history_item(bridge._message_history[-2]) == "q2"
        assert _content_of_history_item(bridge._message_history[-1]) == "Hello world"

        # Interruption truncates *this* turn; the prior turn is untouched.
        bridge.apply_interruption("Hello world", CancellationMode.IMMEDIATE_STOP)
        assert _content_of_history_item(bridge._message_history[-1]) == "Hello world..."
        assert _content_of_history_item(bridge._message_history[1]) == "a1"

    @pytest.mark.asyncio
    async def test_early_cancel_before_any_token_leaves_prior_reply_intact(self):
        """Cancelled before the first token: only the user message is
        recorded, so a follow-up ``apply_interruption("")`` must no-op
        rather than walk back and overwrite the *previous* turn's reply."""

        class _HangingRunnable:
            async def astream_events(
                self, input: Any, **kwargs: Any
            ) -> AsyncIterator[dict[str, Any]]:
                yield {
                    "event": "on_chat_model_start",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": [],
                    "data": {},
                }
                await asyncio.Event().wait()

            async def ainvoke(self, *args: Any, **kwargs: Any) -> Any: ...

        bridge = LangChainBridge(_HangingRunnable())
        bridge._message_history = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]
        runner = AgentRunner(bridge, AgentRunnerConfig(timeout=0.05))

        with pytest.raises(AgentTimeoutError):
            async for _ in runner.invoke(AgentTurnInput.from_text("q2"), _recorder()):
                pass

        # Only the user message was recorded for the cancelled turn.
        assert _content_of_history_item(bridge._message_history[-1]) == "q2"
        assert _role_of_msg(bridge._message_history[-1]) in ("user", "human")

        # apply_interruption("") must not reach back into the prior turn.
        bridge.apply_interruption("", CancellationMode.IMMEDIATE_STOP)
        assert _content_of_history_item(bridge._message_history[1]) == "a1"
        # No phantom assistant message was injected either.
        assert len(bridge._message_history) == 3


class TestLangChainBridgeCancelTokenStoreMirror:
    """A cancel token tripped mid-stream (barge-in) breaks out through
    the *normal* completion path, not the ``BaseException`` cleanup.  The
    wrapped ``RunnableWithMessageHistory`` save listener never fired, so
    the partial turn must still be mirrored into the backing store there
    — or a follow-up ``apply_interruption()`` rewrites the *previous*
    turn's stored assistant message."""

    class _CancelAfter:
        """``is_cancelled`` is ``False`` for the first ``n`` checks then
        ``True`` — a barge-in tripped after some text has streamed."""

        def __init__(self, n: int) -> None:
            self._remaining = n

        @property
        def is_cancelled(self) -> bool:
            if self._remaining > 0:
                self._remaining -= 1
                return False
            return True

    @pytest.mark.asyncio
    async def test_partial_mirrored_into_store_on_cancel_token_break(self):
        class _TwoChunkHistoryRunnable:
            def __init__(self) -> None:
                self.history_factory_config: list[Any] = []
                self._stores: dict[str, _InMemoryStore] = {}

            def get_session_history(self, session_id: str) -> _InMemoryStore:
                return self._stores.setdefault(session_id, _InMemoryStore())

            async def astream_events(
                self, input: Any, **kwargs: Any
            ) -> AsyncIterator[dict[str, Any]]:
                # Two chunks so the loop runs a 2nd iteration where the
                # cancel token is observed; the wrapper's end-of-run save
                # listener never fires (the run is abandoned mid-stream).
                yield {
                    "event": "on_chat_model_stream",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": [],
                    "data": {"chunk": _MockAIMessageChunk(content="Hello world")},
                }
                yield {
                    "event": "on_chat_model_stream",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": [],
                    "data": {"chunk": _MockAIMessageChunk(content=" suppressed")},
                }

            async def ainvoke(self, *args: Any, **kwargs: Any) -> Any: ...

        runnable = _TwoChunkHistoryRunnable()
        # A prior, completed turn already persisted in the real store.
        store = runnable.get_session_history("s1")  # _recorder() → session_id="s1"
        store.add_message({"role": "user", "content": "q1"})
        store.add_message({"role": "assistant", "content": "a1"})

        bridge = LangChainBridge(runnable)
        token = self._CancelAfter(1)  # trips on the 2nd loop check
        async for _ in bridge.invoke(
            AgentTurnInput.from_text("q2"), _recorder(), cancel_token=token
        ):
            pass

        # The partial turn was mirrored into the wrapped store...
        assert _content_of_history_item(store.messages[-2]) == "q2"
        assert _content_of_history_item(store.messages[-1]) == "Hello world"
        # ...so apply_interruption() truncates *this* turn in the store,
        # leaving the prior turn's assistant message intact.
        bridge.apply_interruption("Hello world", CancellationMode.IMMEDIATE_STOP)
        assert _content_of_history_item(store.messages[-1]) == "Hello world..."
        assert _content_of_history_item(store.messages[1]) == "a1"
