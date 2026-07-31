"""LangChain bridge construction, invoke, structured output, and message input tests."""

from __future__ import annotations

from ._langchain_bridge_support import (
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
    LangChainBridge,
    UnitKind,
    _content_of_history_item,
    _MockAIMessageChunk,
    _MockRunnable,
    _recorder,
    _role_of_msg,
    asyncio,
    pytest,
)


class TestLangChainBridgeConstruction:
    def test_rejects_none(self):
        with pytest.raises(BridgeInputError):
            LangChainBridge(None)  # type: ignore[arg-type]

    def test_rejects_non_runnable(self):
        with pytest.raises(BridgeInputError):

            class NotARunnable:
                pass

            LangChainBridge(NotARunnable())

    def test_rejects_ainvoke_only_runnable(self):
        """``invoke()`` drives the underlying runnable via
        ``astream_events``, so an object that implements ``ainvoke`` but
        not ``astream_events`` would crash on the first turn.  Reject it
        at construction instead."""

        class AinvokeOnly:
            async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
                return "ok"

        with pytest.raises(BridgeInputError):
            LangChainBridge(AinvokeOnly())

    def test_committable_boundaries_published(self):
        assert LangChainBridge.COMMITTABLE_BOUNDARIES
        assert LangChainBridge.COMMITTABLE_BOUNDARIES[UnitKind.AGENT] == CommitRule.BETWEEN_TURNS


class TestLangChainBridgeInvoke:
    @pytest.mark.asyncio
    async def test_streams_text_and_emits_done(self):
        chunk = _MockAIMessageChunk(content="hello ")
        chunk2 = _MockAIMessageChunk(content="world")
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chain_start",
                    "name": "RunnableSequence",
                    "run_id": "c",
                    "parent_ids": [],
                    "data": {},
                },
                {
                    "event": "on_chat_model_start",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": ["c"],
                    "data": {},
                },
                {
                    "event": "on_chat_model_stream",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": ["c"],
                    "data": {"chunk": chunk},
                },
                {
                    "event": "on_chat_model_stream",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": ["c"],
                    "data": {"chunk": chunk2},
                },
                {
                    "event": "on_chat_model_end",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": ["c"],
                    "data": {"output": _MockAIMessageChunk(content="hello world")},
                },
                {
                    "event": "on_chain_end",
                    "name": "RunnableSequence",
                    "run_id": "c",
                    "parent_ids": [],
                    "data": {"output": "hello world"},
                },
            ]
        )
        bridge = LangChainBridge(runnable)

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            events.append(ev)

        text_events = [e for e in events if e.kind == "text_delta"]
        done = [e for e in events if e.kind == "done"]
        assert "".join(e.text for e in text_events) == "hello world"
        assert len(done) == 1
        assert done[0].structured_output == "hello world"

        records = journal.read()
        names = [r.name for r in records]
        # Cursor surface: outer agent + nested chain + nested model all paired.
        assert names[0] == "unit_entered"
        assert names[-1] == "unit_exited"
        assert names.count("unit_entered") == names.count("unit_exited")

    @pytest.mark.asyncio
    async def test_ignores_non_mapping_stream_events(self):
        """A malformed provider event must not abort a later valid response."""
        runnable = _MockRunnable(
            [
                ["not an event object"],
                {
                    "event": "on_chat_model_stream",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": [],
                    "data": {"chunk": _MockAIMessageChunk(content="still works")},
                },
            ]
        )
        bridge = LangChainBridge(runnable)

        events = []
        async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(event)

        text = "".join(event.text for event in events if event.kind == "text_delta")
        assert text == "still works"
        assert [event.kind for event in events][-1] == "done"

    @pytest.mark.asyncio
    async def test_parallel_model_runs_do_not_violate_recorder_stack(self):
        """``RunnableParallel`` can start two chat-model runs before either
        finishes, so the recorder's strict LIFO closure has to tolerate an
        ``on_chat_model_end`` arriving while a sibling cursor is still the
        stack top.  The bridge defers each non-top close until the
        obstructing sibling(s) end so the recorder doesn't raise
        ``RecorderInvariantError`` mid-turn."""
        chunk_a = _MockAIMessageChunk(content="A")
        chunk_b = _MockAIMessageChunk(content="B")
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chat_model_start",
                    "name": "ChatA",
                    "run_id": "m-a",
                    "parent_ids": [],
                    "data": {},
                },
                {
                    "event": "on_chat_model_start",
                    "name": "ChatB",
                    "run_id": "m-b",
                    "parent_ids": [],
                    "data": {},
                },
                {
                    "event": "on_chat_model_stream",
                    "name": "ChatA",
                    "run_id": "m-a",
                    "parent_ids": [],
                    "data": {"chunk": chunk_a},
                },
                {
                    "event": "on_chat_model_stream",
                    "name": "ChatB",
                    "run_id": "m-b",
                    "parent_ids": [],
                    "data": {"chunk": chunk_b},
                },
                # ``m-a`` ends first while ``m-b`` is still on top of the
                # recorder stack — naive ``record_unit_exited`` here would
                # raise ``RecorderInvariantError``.
                {
                    "event": "on_chat_model_end",
                    "name": "ChatA",
                    "run_id": "m-a",
                    "parent_ids": [],
                    "data": {"output": chunk_a},
                },
                {
                    "event": "on_chat_model_end",
                    "name": "ChatB",
                    "run_id": "m-b",
                    "parent_ids": [],
                    "data": {"output": chunk_b},
                },
            ]
        )
        bridge = LangChainBridge(runnable)
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            events.append(ev)

        text = "".join(e.text for e in events if e.kind == "text_delta")
        assert text == "AB"

        records = journal.read()
        names = [r.name for r in records]
        # Both model cursors entered and exited, paired with the outer
        # agent cursor.  No invariant errors raised.
        assert names.count("unit_entered") == names.count("unit_exited") == 3
        # Exit order is LIFO: agent encloses both models, and ``m-b``
        # (top of stack) closes before ``m-a`` even though ``m-a`` ended
        # first chronologically.
        exit_records = [r for r in records if r.name == "unit_exited"]
        exit_ids = [r.data["unit_id"] for r in exit_records]
        assert exit_ids == ["model-m-b", "model-m-a", exit_ids[-1]]
        assert exit_ids[-1].startswith("agent-")

    @pytest.mark.asyncio
    async def test_cancel_token_short_circuits(self):
        chunk = _MockAIMessageChunk(content="will-never-emit")
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chat_model_stream",
                    "data": {"chunk": chunk},
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": [],
                },
            ]
        )
        bridge = LangChainBridge(runnable)
        token = CancelToken()
        token.cancel()

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), rec, cancel_token=token):
            events.append(ev)

        assert not any(e.kind == "text_delta" for e in events)
        records = journal.read()
        assert any(r.name == "cancellation_boundary" for r in records)

    @pytest.mark.asyncio
    async def test_tool_call_chunks_flow_into_journal(self):
        chunk = _MockAIMessageChunk(
            content="",
            tool_call_chunks=[
                {"name": "weather", "args": None, "id": "c1", "index": 0},
                {"name": None, "args": '{"q":"x"}', "id": "c1", "index": 0},
            ],
        )
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chat_model_stream",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": [],
                    "data": {"chunk": chunk},
                },
            ]
        )
        bridge = LangChainBridge(runnable)
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            pass

        phases = [r.data["phase"] for r in journal.read() if r.name == "tool_phase_changed"]
        assert "start" in phases
        assert "delta" in phases

    @pytest.mark.asyncio
    async def test_chain_only_runnable_emits_text(self):
        """``RunnableLambda``-style chains have no chat_model so they only
        surface text through ``on_chain_stream`` chunks.  The default
        ``include_types`` must keep ``chain`` so these don't silently
        produce empty ``done`` events."""
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chain_start",
                    "name": "RunnableLambda",
                    "run_id": "l1",
                    "parent_ids": [],
                    "data": {},
                },
                {
                    "event": "on_chain_stream",
                    "name": "RunnableLambda",
                    "run_id": "l1",
                    "parent_ids": [],
                    "data": {"chunk": "hello "},
                },
                {
                    "event": "on_chain_stream",
                    "name": "RunnableLambda",
                    "run_id": "l1",
                    "parent_ids": [],
                    "data": {"chunk": "world"},
                },
                {
                    "event": "on_chain_end",
                    "name": "RunnableLambda",
                    "run_id": "l1",
                    "parent_ids": [],
                    "data": {"output": "hello world"},
                },
            ]
        )
        bridge = LangChainBridge(runnable)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)
        text = "".join(e.text for e in events if e.kind == "text_delta")
        done = [e for e in events if e.kind == "done"]
        assert text == "hello world"
        assert done and done[0].text == "hello world"

    @pytest.mark.asyncio
    async def test_nested_lambda_chain_does_not_double_speak(self):
        """``RunnableLambda(f) | RunnableLambda(g)`` with no model
        descendant: LangChain emits a chain stream for child ``f``
        (``"a"``), child ``g`` (``"ab"``) and the parent that forwards
        the composed result (``"ab"``).  Speaking every chunk would
        narrate ``"a" + "ab" + "ab"``; only the final ``"ab"`` is the
        real answer."""
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chain_start",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {},
                },
                {
                    "event": "on_chain_start",
                    "name": "RunnableLambda",
                    "run_id": "f",
                    "parent_ids": ["seq"],
                    "data": {},
                },
                {
                    "event": "on_chain_stream",
                    "name": "RunnableLambda",
                    "run_id": "f",
                    "parent_ids": ["seq"],
                    "data": {"chunk": "a"},
                },
                {
                    "event": "on_chain_end",
                    "name": "RunnableLambda",
                    "run_id": "f",
                    "parent_ids": ["seq"],
                    "data": {"output": "a"},
                },
                {
                    "event": "on_chain_start",
                    "name": "RunnableLambda",
                    "run_id": "g",
                    "parent_ids": ["seq"],
                    "data": {},
                },
                {
                    "event": "on_chain_stream",
                    "name": "RunnableLambda",
                    "run_id": "g",
                    "parent_ids": ["seq"],
                    "data": {"chunk": "ab"},
                },
                {
                    "event": "on_chain_end",
                    "name": "RunnableLambda",
                    "run_id": "g",
                    "parent_ids": ["seq"],
                    "data": {"output": "ab"},
                },
                {
                    "event": "on_chain_stream",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {"chunk": "ab"},
                },
                {
                    "event": "on_chain_end",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {"output": "ab"},
                },
            ]
        )
        bridge = LangChainBridge(runnable)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)
        text = "".join(e.text for e in events if e.kind == "text_delta")
        done = [e for e in events if e.kind == "done"]
        assert text == "ab"
        assert done and done[0].text == "ab"

    @pytest.mark.asyncio
    async def test_agent_runner_timeout_closes_open_cursors(self):
        """The default ``AgentRunner`` enforces its timeout by
        cancelling the bridge's pending ``__anext__``
        (``asyncio.CancelledError``) and then ``aclose()``-ing it
        (``GeneratorExit``).  Neither is an ``Exception``, so the
        ``except Exception`` cleanup is skipped — the agent + model
        cursors opened before the hang must still get ``unit_exited``
        records or the recorder's stack invariant breaks for the
        postmortem journal."""

        class _HangingRunnable:
            async def astream_events(
                self, input: Any, **kwargs: Any
            ) -> AsyncIterator[dict[str, Any]]:
                yield {
                    "event": "on_chain_start",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {},
                }
                yield {
                    "event": "on_chat_model_start",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": ["seq"],
                    "data": {},
                }
                await asyncio.Event().wait()
                yield {  # pragma: no cover — cancelled before this fires
                    "event": "on_chain_end",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {},
                }

            async def ainvoke(self, *args: Any, **kwargs: Any) -> Any: ...

        bridge = LangChainBridge(_HangingRunnable())
        runner = AgentRunner(bridge, AgentRunnerConfig(timeout=0.05))
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        with pytest.raises(AgentTimeoutError):
            async for _ in runner.invoke(AgentTurnInput.from_text("hi"), rec):
                pass

        names = [r.name for r in journal.read()]
        assert names.count("unit_entered") == names.count("unit_exited") == 2

    @pytest.mark.asyncio
    async def test_consumer_aclose_closes_underlying_event_stream(self):
        """Barge-in must close the runnable-owned async iterator too."""

        class _CloseAwareEvents:
            def __init__(self) -> None:
                self.closed = False
                self._events = iter(
                    [
                        {
                            "event": "on_chat_model_stream",
                            "name": "ChatOpenAI",
                            "run_id": "m",
                            "parent_ids": [],
                            "data": {"chunk": _MockAIMessageChunk(content="Hello")},
                        }
                    ]
                )

            def __aiter__(self):
                return self

            async def __anext__(self) -> dict[str, Any]:
                try:
                    return next(self._events)
                except StopIteration as exc:
                    raise StopAsyncIteration from exc

            async def aclose(self) -> None:
                self.closed = True

        class _CloseAwareRunnable:
            def __init__(self, events: _CloseAwareEvents) -> None:
                self.events = events

            def astream_events(self, input: Any, **kwargs: Any) -> _CloseAwareEvents:
                return self.events

        events = _CloseAwareEvents()
        bridge = LangChainBridge(_CloseAwareRunnable(events))
        stream = bridge.invoke(AgentTurnInput.from_text("hi"), _recorder())

        first = await anext(stream)
        assert first.kind == "text_delta"
        assert first.text == "Hello"
        await stream.aclose()

        assert events.closed

    @pytest.mark.asyncio
    async def test_chain_wrapping_text_llm_streams_text(self):
        """Chains like ``PromptTemplate | FakeStreamingListLLM`` use a
        non-chat ``BaseLLM`` whose raw tokens surface via
        ``on_llm_stream``.  The bridge should prefer the parent chain's
        composed stream so downstream transforms/redactors remain
        authoritative."""

        class _GenerationChunk:
            def __init__(self, text: str) -> None:
                self.text = text

        runnable = _MockRunnable(
            [
                {
                    "event": "on_chain_start",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {},
                },
                {
                    "event": "on_llm_start",
                    "name": "FakeStreamingListLLM",
                    "run_id": "l",
                    "parent_ids": ["seq"],
                    "data": {},
                },
                {
                    "event": "on_llm_stream",
                    "name": "FakeStreamingListLLM",
                    "run_id": "l",
                    "parent_ids": ["seq"],
                    "data": {"chunk": _GenerationChunk("hello ")},
                },
                {
                    "event": "on_llm_stream",
                    "name": "FakeStreamingListLLM",
                    "run_id": "l",
                    "parent_ids": ["seq"],
                    "data": {"chunk": _GenerationChunk("world")},
                },
                {
                    "event": "on_chain_stream",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {"chunk": "hello world"},
                },
                {
                    "event": "on_llm_end",
                    "name": "FakeStreamingListLLM",
                    "run_id": "l",
                    "parent_ids": ["seq"],
                    "data": {"output": "hello world"},
                },
                {
                    "event": "on_chain_end",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {"output": "hello world"},
                },
            ]
        )
        bridge = LangChainBridge(runnable)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("bob"), _recorder()):
            events.append(ev)
        text = "".join(e.text for e in events if e.kind == "text_delta")
        done = [e for e in events if e.kind == "done"]
        assert text == "hello world"
        assert done and done[0].text == "hello world"

    @pytest.mark.asyncio
    async def test_chain_wrapping_non_streaming_llm_emits_text(self):
        """``FakeStreamingListLLM`` and similar non-streaming ``BaseLLM``
        subclasses don't override ``_stream`` — LangChain emits only
        ``on_llm_end`` (with the full ``LLMResult``) and the chain's
        per-character ``on_chain_stream`` chunks fire afterwards.  The
        bridge should emit the composed chain stream, not the raw LLM
        result, when the LLM is parented by a chain."""
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chain_start",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {},
                },
                {
                    "event": "on_llm_start",
                    "name": "FakeStreamingListLLM",
                    "run_id": "l",
                    "parent_ids": ["seq"],
                    "data": {},
                },
                # Real FakeStreamingListLLM emits NO on_llm_stream events,
                # only on_llm_end with the full LLMResult payload.
                {
                    "event": "on_llm_end",
                    "name": "FakeStreamingListLLM",
                    "run_id": "l",
                    "parent_ids": ["seq"],
                    "data": {
                        "output": {
                            "generations": [[{"text": "hello world", "type": "Generation"}]],
                            "llm_output": None,
                        }
                    },
                },
                # Chain then forwards the LLM output character-by-character
                # via on_chain_stream — those must stay suppressed so we
                # don't double-emit on top of the on_llm_end text.
                *[
                    {
                        "event": "on_chain_stream",
                        "name": "RunnableSequence",
                        "run_id": "seq",
                        "parent_ids": [],
                        "data": {"chunk": ch},
                    }
                    for ch in "hello world"
                ],
                {
                    "event": "on_chain_end",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {"output": "hello world"},
                },
            ]
        )
        bridge = LangChainBridge(runnable)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("bob"), _recorder()):
            events.append(ev)
        text = "".join(e.text for e in events if e.kind == "text_delta")
        done = [e for e in events if e.kind == "done"]
        # Exactly one emission from the composed chain stream.
        assert text == "hello world"
        assert done and done[0].text == "hello world"

    @pytest.mark.asyncio
    async def test_parented_text_llm_uses_redacted_chain_stream(self):
        """Raw ``on_llm_stream`` text from a BaseLLM inside a chain must
        not bypass a downstream redactor.  The public text stream should
        come from the root chain's composed output instead."""

        class _GenerationChunk:
            def __init__(self, text: str) -> None:
                self.text = text

        runnable = _MockRunnable(
            [
                {
                    "event": "on_chain_start",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {},
                },
                {
                    "event": "on_llm_start",
                    "name": "FakeStreamingListLLM",
                    "run_id": "l",
                    "parent_ids": ["seq"],
                    "data": {},
                },
                {
                    "event": "on_llm_stream",
                    "name": "FakeStreamingListLLM",
                    "run_id": "l",
                    "parent_ids": ["seq"],
                    "data": {"chunk": _GenerationChunk("SECRET_TOKEN=abc123")},
                },
                {
                    "event": "on_chain_stream",
                    "name": "RunnableLambda",
                    "run_id": "redactor",
                    "parent_ids": ["seq"],
                    "data": {"chunk": "[REDACTED]"},
                },
                {
                    "event": "on_chain_stream",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {"chunk": "[REDACTED]"},
                },
                {
                    "event": "on_llm_end",
                    "name": "FakeStreamingListLLM",
                    "run_id": "l",
                    "parent_ids": ["seq"],
                    "data": {"output": "SECRET_TOKEN=abc123"},
                },
                {
                    "event": "on_chain_end",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {"output": "[REDACTED]"},
                },
            ]
        )

        bridge = LangChainBridge(runnable)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("bob"), _recorder()):
            events.append(ev)

        streamed = "".join(e.text for e in events if e.kind == "text_delta")
        done = [e for e in events if e.kind == "done"]
        assert streamed == "[REDACTED]"
        assert done and done[0].text == "[REDACTED]"
        ai_msgs = [
            m
            for m in bridge._message_history
            if getattr(m, "type", None) == "ai"
            or (isinstance(m, dict) and m.get("role") == "assistant")
        ]
        assert ai_msgs and _content_of_history_item(ai_msgs[-1]) == "[REDACTED]"

    @pytest.mark.asyncio
    async def test_parented_non_streaming_text_llm_uses_selected_chain_output(self):
        """Parented ``on_llm_end`` should not concatenate raw generation
        candidates when the root chain selected a sanitized final output."""
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chain_start",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {},
                },
                {
                    "event": "on_llm_start",
                    "name": "FakeStreamingListLLM",
                    "run_id": "l",
                    "parent_ids": ["seq"],
                    "data": {},
                },
                {
                    "event": "on_llm_end",
                    "name": "FakeStreamingListLLM",
                    "run_id": "l",
                    "parent_ids": ["seq"],
                    "data": {
                        "output": {
                            "generations": [
                                [
                                    {"text": "SECRET_TOKEN=abc123", "type": "Generation"},
                                    {"text": "ALT_SECRET=def456", "type": "Generation"},
                                ]
                            ],
                            "llm_output": None,
                        }
                    },
                },
                {
                    "event": "on_chain_stream",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {"chunk": "[REDACTED]"},
                },
                {
                    "event": "on_chain_end",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {"output": "[REDACTED]"},
                },
            ]
        )

        bridge = LangChainBridge(runnable)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("bob"), _recorder()):
            events.append(ev)

        streamed = "".join(e.text for e in events if e.kind == "text_delta")
        done = [e for e in events if e.kind == "done"]
        assert streamed == "[REDACTED]"
        assert done and done[0].text == "[REDACTED]"

    @pytest.mark.asyncio
    async def test_chain_wrapping_chat_model_does_not_double_emit(self):
        """When a chain wraps a ``chat_model``, its ``on_chain_stream``
        chunks forward the same tokens already emitted via
        ``on_chat_model_stream``.  Emitting both would speak each token
        twice — the bridge must deduplicate using the chat_model's
        parent_ids."""
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chain_start",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {},
                },
                {
                    "event": "on_chat_model_start",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": ["seq"],
                    "data": {},
                },
                {
                    "event": "on_chat_model_stream",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": ["seq"],
                    "data": {"chunk": _MockAIMessageChunk(content="hi!")},
                },
                {
                    "event": "on_chain_stream",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {"chunk": _MockAIMessageChunk(content="hi!")},
                },
                {
                    "event": "on_chat_model_end",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": ["seq"],
                    "data": {"output": _MockAIMessageChunk(content="hi!")},
                },
                {
                    "event": "on_chain_end",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {"output": "hi!"},
                },
            ]
        )
        bridge = LangChainBridge(runnable)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)
        text = "".join(e.text for e in events if e.kind == "text_delta")
        assert text == "hi!"

    @pytest.mark.asyncio
    async def test_chain_with_downstream_parser_does_not_double_emit(self):
        """``prompt | chat_model | StrOutputParser() | RunnableLambda(...)``
        emits the model tokens via ``on_chat_model_stream`` *and* the
        same content via downstream-sibling ``on_chain_stream`` events
        (parser/lambda restating the parsed text).  Without suppression
        the bridge speaks ``abcABC`` — once from the model, once from
        each downstream stage."""
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chain_start",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {},
                },
                {
                    "event": "on_chat_model_start",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": ["seq"],
                    "data": {},
                },
                {
                    "event": "on_chat_model_stream",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": ["seq"],
                    "data": {"chunk": _MockAIMessageChunk(content="abc")},
                },
                # StrOutputParser is a sibling of the model under ``seq``
                # and re-yields the parsed string.
                {
                    "event": "on_chain_start",
                    "name": "StrOutputParser",
                    "run_id": "parser",
                    "parent_ids": ["seq"],
                    "data": {},
                },
                {
                    "event": "on_chain_stream",
                    "name": "StrOutputParser",
                    "run_id": "parser",
                    "parent_ids": ["seq"],
                    "data": {"chunk": "abc"},
                },
                {
                    "event": "on_chain_end",
                    "name": "StrOutputParser",
                    "run_id": "parser",
                    "parent_ids": ["seq"],
                    "data": {"output": "abc"},
                },
                # RunnableLambda is also a sibling of the model under
                # ``seq``; it transforms the parsed string and would
                # otherwise double-emit on top of the model stream.
                {
                    "event": "on_chain_start",
                    "name": "RunnableLambda",
                    "run_id": "lambda",
                    "parent_ids": ["seq"],
                    "data": {},
                },
                {
                    "event": "on_chain_stream",
                    "name": "RunnableLambda",
                    "run_id": "lambda",
                    "parent_ids": ["seq"],
                    "data": {"chunk": "ABC"},
                },
                {
                    "event": "on_chain_end",
                    "name": "RunnableLambda",
                    "run_id": "lambda",
                    "parent_ids": ["seq"],
                    "data": {"output": "ABC"},
                },
                {
                    "event": "on_chat_model_end",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": ["seq"],
                    "data": {"output": _MockAIMessageChunk(content="abc")},
                },
                {
                    "event": "on_chain_end",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {"output": "ABC"},
                },
            ]
        )
        bridge = LangChainBridge(runnable)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)
        text = "".join(e.text for e in events if e.kind == "text_delta")
        # Only the chat_model stream contributes; downstream parser /
        # lambda chain streams are siblings of the model and would
        # otherwise duplicate its tokens.
        assert text == "abc"

    @pytest.mark.asyncio
    async def test_downstream_transform_overrides_done_text_and_history(self):
        """``model | StrOutputParser() | RunnableLambda(str.upper)``:
        the transforming downstream sibling's ``on_chain_stream`` is
        suppressed (it would double-speak the model tokens), so the
        streamed text is the raw lowercase model output.  The final
        ``done.text`` and next-turn history must instead be the
        top-level chain's real transformed output, not the unmodified
        internal model tokens."""
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chain_start",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {},
                },
                {
                    "event": "on_chat_model_start",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": ["seq"],
                    "data": {},
                },
                {
                    "event": "on_chat_model_stream",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": ["seq"],
                    "data": {"chunk": _MockAIMessageChunk(content="abc")},
                },
                {
                    "event": "on_chain_stream",
                    "name": "RunnableLambda",
                    "run_id": "lambda",
                    "parent_ids": ["seq"],
                    "data": {"chunk": "ABC"},
                },
                {
                    "event": "on_chat_model_end",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": ["seq"],
                    "data": {"output": _MockAIMessageChunk(content="abc")},
                },
                {
                    "event": "on_chain_end",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {"output": "ABC"},
                },
            ]
        )
        bridge = LangChainBridge(runnable)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)

        # Live stream is the raw model tokens (downstream transform
        # suppressed to avoid double-speak).
        streamed = "".join(e.text for e in events if e.kind == "text_delta")
        assert streamed == "abc"

        # ...but the recorded final answer + history are the chain's
        # real transformed output.
        done = [e for e in events if e.kind == "done"]
        assert done and done[0].text == "ABC"
        assert done[0].structured_output == "ABC"
        ai_msgs = [
            m
            for m in bridge._message_history
            if getattr(m, "type", None) == "ai"
            or (isinstance(m, dict) and m.get("role") == "assistant")
        ]
        assert ai_msgs and _content_of_history_item(ai_msgs[-1]) == "ABC"

    @pytest.mark.asyncio
    async def test_chain_wrapping_non_streaming_chat_model_emits_text(self):
        """Non-streaming chat models (any chat model that doesn't override
        ``_stream`` / ``_astream``) skip ``on_chat_model_stream`` and only
        surface their AIMessage via ``on_chat_model_end``.  The parent
        chain re-yields the same AIMessage through ``on_chain_stream``,
        which the bridge suppresses — without the end-of-model fallback
        the assistant goes silent and history records an empty turn."""
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chain_start",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {},
                },
                {
                    "event": "on_chat_model_start",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": ["seq"],
                    "data": {},
                },
                {
                    "event": "on_chat_model_end",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": ["seq"],
                    "data": {"output": _MockAIMessageChunk(content="hello world")},
                },
                # Parent chain re-yields the AIMessage — must be suppressed
                # so we don't double-emit on top of the end-of-model text.
                {
                    "event": "on_chain_stream",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {"chunk": _MockAIMessageChunk(content="hello world")},
                },
                {
                    "event": "on_chain_end",
                    "name": "RunnableSequence",
                    "run_id": "seq",
                    "parent_ids": [],
                    "data": {"output": "hello world"},
                },
            ]
        )
        bridge = LangChainBridge(runnable)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)
        text = "".join(e.text for e in events if e.kind == "text_delta")
        done = [e for e in events if e.kind == "done"]
        # End-of-model fallback emits exactly once; chain stream suppressed.
        assert text == "hello world"
        assert done and done[0].text == "hello world"
        # History records the assistant turn — empty text would skip the
        # AIMessage append and leave the next turn without context.
        assert any(_content_of_history_item(m) == "hello world" for m in bridge._message_history)

    @pytest.mark.asyncio
    async def test_turn_context_flows_into_history_payload(self):
        """Per-turn system/developer context (caller-id, system prefix,
        explicit ``AgentTurnInput.context``) must reach the runnable's
        prompt — dropping it silently makes session instructions
        invisible to LangChain agents."""
        runnable = _MockRunnable([])
        bridge = LangChainBridge(runnable)
        turn = AgentTurnInput.from_text(
            "what time is it?",
            context=[
                {"role": "system", "content": "Caller id: +15551234"},
                # ``user`` items from the caller are filtered out because
                # the bridge owns its own history.
                {"role": "user", "content": "this should be dropped"},
            ],
        )
        async for _ in bridge.invoke(turn, _recorder()):
            pass
        payload = runnable.invoked_with[0]
        assert isinstance(payload, dict)
        assert payload["input"] == "what time is it?"
        history = payload["history"]
        assert len(history) == 1  # system only — user dropped, no prior turns yet
        assert _content_of_history_item(history[0]) == "Caller id: +15551234"

    @pytest.mark.asyncio
    async def test_history_roundtrip(self):
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chat_model_stream",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": [],
                    "data": {"chunk": _MockAIMessageChunk(content="hi!")},
                }
            ]
        )
        bridge = LangChainBridge(runnable)
        rec = _recorder()
        async for _ in bridge.invoke(AgentTurnInput.from_text("hello"), rec):
            pass
        assert len(bridge._message_history) == 2  # 1 human + 1 ai
        # Next call should see non-empty history key in input payload.
        async for _ in bridge.invoke(AgentTurnInput.from_text("again"), rec):
            pass
        payload = runnable.invoked_with[0]
        assert isinstance(payload, dict)
        assert payload["input"] == "again"
        assert len(payload["history"]) == 2


class TestLangChainBridgeStructuredOutput:
    @pytest.mark.asyncio
    async def test_structured_only_runnable_preserves_chain_output(self):
        """Runnables that return a structured value without streaming
        text chunks (``RunnableLambda(lambda _: {"answer": 42})``,
        ``with_structured_output(...)``) must surface that value as
        ``done.structured_output`` — falling back to the empty
        accumulated text would silently strip the result."""
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chain_start",
                    "name": "RunnableLambda",
                    "run_id": "c1",
                    "parent_ids": [],
                    "data": {},
                },
                {
                    "event": "on_chain_stream",
                    "name": "RunnableLambda",
                    "run_id": "c1",
                    "parent_ids": [],
                    "data": {"chunk": {"answer": 42}},
                },
                {
                    "event": "on_chain_end",
                    "name": "RunnableLambda",
                    "run_id": "c1",
                    "parent_ids": [],
                    "data": {"output": {"answer": 42}},
                },
            ]
        )
        bridge = LangChainBridge(runnable)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)
        done = [e for e in events if e.kind == "done"]
        assert done and done[0].structured_output == {"answer": 42}
        assert done[0].text == ""  # no text chunks streamed

    @pytest.mark.asyncio
    async def test_dict_output_chain_speaks_answer_when_nothing_streamed(self):
        """An ``AgentExecutor`` / ``return_direct`` tool finishes with
        ``{"output": "..."}`` and never streams a model text token (its
        chain streams are suppressed as a model-descendant).  ``done.text``
        is the consumer's only spoken text and must carry the real answer
        — not the empty accumulated string."""
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chain_start",
                    "name": "AgentExecutor",
                    "run_id": "agent",
                    "parent_ids": [],
                    "data": {},
                },
                {
                    # Marks "agent" as a chain with a model descendant, so
                    # its forwarded {"output": ...} chain stream below is
                    # suppressed (would otherwise double-speak).
                    "event": "on_chat_model_start",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": ["agent"],
                    "data": {},
                },
                {
                    "event": "on_chain_stream",
                    "name": "AgentExecutor",
                    "run_id": "agent",
                    "parent_ids": [],
                    "data": {"chunk": {"output": "the answer"}},
                },
                {
                    "event": "on_chain_end",
                    "name": "AgentExecutor",
                    "run_id": "agent",
                    "parent_ids": [],
                    "data": {"output": {"output": "the answer"}},
                },
            ]
        )
        bridge = LangChainBridge(runnable)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)
        # Nothing streamed (chain stream suppressed as a model-descendant,
        # no model text token), so there is no double-speak and the answer
        # rides only on done.text — the consumer's nothing-streamed
        # fallback — instead of the previously-empty string.
        assert [e for e in events if e.kind == "text_delta"] == []
        done = [e for e in events if e.kind == "done"]
        assert done and done[0].text == "the answer"
        assert done[0].structured_output == {"output": "the answer"}

    @pytest.mark.asyncio
    async def test_dispatch_custom_event_drives_text_delta_by_default(self):
        """LCEL ``dispatch_custom_event`` payloads must reach the
        translator under the default include_types — narrowing the
        filter was silently disabling the custom-event TTS path."""
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chain_start",
                    "name": "RunnableLambda",
                    "run_id": "c1",
                    "parent_ids": [],
                    "data": {},
                },
                {
                    "event": "on_custom_event",
                    "name": "status",
                    "run_id": "c1",
                    "parent_ids": [],
                    "data": {"text": "thinking..."},
                },
                {
                    "event": "on_chain_end",
                    "name": "RunnableLambda",
                    "run_id": "c1",
                    "parent_ids": [],
                    "data": {"output": None},
                },
            ]
        )
        bridge = LangChainBridge(runnable)
        # Default include_types must not be passed to astream_events as a
        # narrow tuple — otherwise LangChain drops on_custom_event.
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)
        text_events = [e for e in events if e.kind == "text_delta"]
        assert text_events and text_events[0].text == "thinking..."
        # Confirm the bridge did not silently re-add a filter that would
        # strip the event upstream.
        assert "include_types" not in runnable.invoked_with[1]

    @pytest.mark.asyncio
    async def test_tool_calls_emit_single_started_per_call(self):
        """For tool-calling agents that surface both ``tool_call_chunks``
        (model decision) and ``on_tool_start`` (framework invocation),
        the bridge must only emit one ``tool_started`` per logical call
        so downstream tool_started/tool_result accounting stays balanced.
        The matching ``on_tool_end`` must reuse the provider call id so
        the pair is mapped to a single call."""
        chunk = _MockAIMessageChunk(
            content="",
            tool_call_chunks=[
                {"name": "get_weather", "args": '{"city":"Tokyo"}', "id": "call-abc", "index": 0},
            ],
        )
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chat_model_stream",
                    "name": "ChatOpenAI",
                    "run_id": "m1",
                    "parent_ids": [],
                    "data": {"chunk": chunk},
                },
                {
                    "event": "on_tool_start",
                    "name": "get_weather",
                    "run_id": "tool-run-xyz",
                    "parent_ids": [],
                    "data": {"input": {"city": "Tokyo"}},
                },
                {
                    "event": "on_tool_end",
                    "name": "get_weather",
                    "run_id": "tool-run-xyz",
                    "parent_ids": [],
                    "data": {"output": "Sunny."},
                },
            ]
        )
        bridge = LangChainBridge(runnable)
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(ev)
        started = [e for e in events if e.kind == "tool_started"]
        results = [e for e in events if e.kind == "tool_result"]
        assert len(started) == 1
        assert len(results) == 1
        assert started[0].call_id == "call-abc"
        assert results[0].call_id == "call-abc"


class TestLangChainBridgeMessagesInput:
    """``messages_input=True`` — for bare ``BaseChatModel`` / ``BaseLLM``."""

    @pytest.mark.asyncio
    async def test_passes_message_sequence_not_dict(self):
        """Bare language-model runnables reject dict inputs.  The bridge
        must hand them a message sequence ending with the user turn."""
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chat_model_stream",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": [],
                    "data": {"chunk": _MockAIMessageChunk(content="hi!")},
                }
            ]
        )
        bridge = LangChainBridge(runnable, messages_input=True)
        async for _ in bridge.invoke(AgentTurnInput.from_text("hello"), _recorder()):
            pass
        payload = runnable.invoked_with[0]
        assert isinstance(payload, list)  # not a dict — would crash a chat model
        assert _content_of_history_item(payload[-1]) == "hello"

    @pytest.mark.asyncio
    async def test_threads_history_as_messages(self):
        """History still threads through — as messages, not a dict key."""
        runnable = _MockRunnable(
            [
                {
                    "event": "on_chat_model_stream",
                    "name": "ChatOpenAI",
                    "run_id": "m",
                    "parent_ids": [],
                    "data": {"chunk": _MockAIMessageChunk(content="reply")},
                }
            ]
        )
        bridge = LangChainBridge(runnable, messages_input=True)
        rec = _recorder()
        async for _ in bridge.invoke(AgentTurnInput.from_text("first"), rec):
            pass
        async for _ in bridge.invoke(AgentTurnInput.from_text("second"), rec):
            pass
        payload = runnable.invoked_with[0]
        assert isinstance(payload, list)
        contents = [_content_of_history_item(m) for m in payload]
        # prior user + assistant turn, then the new user turn
        assert contents == ["first", "reply", "second"]
        roles = [_role_of_msg(m) for m in payload]
        assert roles[-1] in ("user", "human")
