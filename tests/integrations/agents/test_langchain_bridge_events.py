"""LangChain stream event translator tests."""

from __future__ import annotations

from ._langchain_bridge_support import (
    Any,
    InMemoryRingBuffer,
    _MockAIMessageChunk,
    _recorder,
    translate_stream_event,
)


class TestStreamEventTranslator:
    def test_on_chat_model_stream_yields_text_delta(self):
        chunk = _MockAIMessageChunk(content="hello ")
        event = {
            "event": "on_chat_model_stream",
            "name": "ChatOpenAI",
            "run_id": "r1",
            "parent_ids": [],
            "data": {"chunk": chunk},
        }
        out = list(translate_stream_event(event))
        assert len(out) == 1
        assert out[0].kind == "text_delta"
        assert out[0].text == "hello "

    def test_content_as_list_extracts_text_blocks(self):
        chunk = _MockAIMessageChunk(
            content=[
                {"type": "text", "text": "A"},
                {"type": "thinking", "thinking": "internal only"},
                {"type": "text", "text": "B"},
            ]
        )
        event = {
            "event": "on_chat_model_stream",
            "name": "ChatAnthropic",
            "run_id": "r1",
            "data": {"chunk": chunk},
        }
        out = list(translate_stream_event(event))
        assert out[0].text == "AB"

    def test_tool_call_chunks_record_start_and_delta(self):
        chunk = _MockAIMessageChunk(
            content="",
            tool_call_chunks=[
                {"name": "get_weather", "args": None, "id": "call-1", "index": 0},
                {"name": None, "args": '{"city":', "id": "call-1", "index": 0},
                {"name": None, "args": '"Tokyo"}', "id": "call-1", "index": 0},
            ],
        )
        event = {
            "event": "on_chat_model_stream",
            "data": {"chunk": chunk},
            "name": "",
            "run_id": "",
        }
        journal = InMemoryRingBuffer(capacity=100)
        rec = _recorder(journal)
        out = list(translate_stream_event(event, rec))
        kinds = [e.kind for e in out]
        assert "tool_started" in kinds
        assert kinds.count("tool_delta") == 2

        records = journal.read()
        phases = [r.data["phase"] for r in records if r.name == "tool_phase_changed"]
        assert "start" in phases
        assert phases.count("delta") == 2

    def test_tool_call_chunks_args_only_continuations_keep_id_and_name(self):
        """Streaming providers (OpenAI, ...) put the tool-call ``id`` /
        ``name`` only on the first ``ToolCallChunk``; later argument
        chunks carry just ``index``.  The translator must back-fill the
        id/name from a per-(run_id, index) cache so ``tool_delta`` events
        stay associated with the originating ``tool_started`` instead of
        getting empty strings — and must not re-announce a second start
        when the back-filled name reappears."""
        state: dict[str, Any] = {}
        first = {
            "event": "on_chat_model_stream",
            "name": "ChatOpenAI",
            "run_id": "m1",
            "data": {
                "chunk": _MockAIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {"name": "get_weather", "args": "", "id": "call-1", "index": 0}
                    ],
                )
            },
        }
        cont1 = {
            "event": "on_chat_model_stream",
            "name": "ChatOpenAI",
            "run_id": "m1",
            "data": {
                "chunk": _MockAIMessageChunk(
                    content="",
                    tool_call_chunks=[{"name": None, "args": '{"city":', "id": None, "index": 0}],
                )
            },
        }
        cont2 = {
            "event": "on_chat_model_stream",
            "name": "ChatOpenAI",
            "run_id": "m1",
            "data": {
                "chunk": _MockAIMessageChunk(
                    content="",
                    tool_call_chunks=[{"name": None, "args": '"Tokyo"}', "id": None, "index": 0}],
                )
            },
        }
        out = (
            list(translate_stream_event(first, state=state))
            + list(translate_stream_event(cont1, state=state))
            + list(translate_stream_event(cont2, state=state))
        )
        started = [e for e in out if e.kind == "tool_started"]
        deltas = [e for e in out if e.kind == "tool_delta"]
        assert len(started) == 1  # only the first chunk announces a start
        assert started[0].tool_name == "get_weather"
        assert started[0].call_id == "call-1"
        # Continuation deltas keep the id+name from the first chunk.
        assert len(deltas) == 2
        assert all(d.tool_name == "get_weather" and d.call_id == "call-1" for d in deltas)
        assert [d.text for d in deltas] == ['{"city":', '"Tokyo"}']

    def test_on_tool_start_and_end(self):
        start = {
            "event": "on_tool_start",
            "name": "get_weather",
            "run_id": "call-xyz",
            "data": {"input": {"city": "Tokyo"}},
        }
        end = {
            "event": "on_tool_end",
            "name": "get_weather",
            "run_id": "call-xyz",
            "data": {"output": "24C"},
        }
        journal = InMemoryRingBuffer(capacity=100)
        rec = _recorder(journal)
        a = list(translate_stream_event(start, rec))
        b = list(translate_stream_event(end, rec))
        assert a[0].kind == "tool_started"
        assert a[0].tool_name == "get_weather"
        assert b[0].kind == "tool_result"
        assert b[0].result == "24C"

        phases = [r.data["phase"] for r in journal.read() if r.name == "tool_phase_changed"]
        assert phases == ["start", "result"]

    def test_on_tool_error(self):
        event = {
            "event": "on_tool_error",
            "name": "failing_tool",
            "run_id": "call-1",
            "data": {},
        }
        journal = InMemoryRingBuffer(capacity=100)
        rec = _recorder(journal)
        out = list(translate_stream_event(event, rec))
        assert out[0].kind == "tool_result"
        assert out[0].reason == "tool_error"

    def test_unknown_event_is_ignored(self):
        out = list(translate_stream_event({"event": "on_retriever_start", "data": {}}))
        assert out == []

    def test_on_chain_stream_str_chunk_yields_text_delta(self):
        """``RunnableLambda``-style chains stream plain strings via
        ``on_chain_stream``; the translator must surface them so non-chat
        runnables can still drive TTS + history."""
        event = {
            "event": "on_chain_stream",
            "name": "RunnableLambda",
            "run_id": "c1",
            "data": {"chunk": "hello world"},
        }
        out = list(translate_stream_event(event))
        assert len(out) == 1
        assert out[0].kind == "text_delta"
        assert out[0].text == "hello world"

    def test_on_chain_stream_ai_message_chunk_yields_text_delta(self):
        """LCEL stages that wrap ``AIMessageChunk`` values in
        ``on_chain_stream`` also stream safely."""
        event = {
            "event": "on_chain_stream",
            "name": "LCEL",
            "run_id": "c1",
            "data": {"chunk": _MockAIMessageChunk(content="delta")},
        }
        out = list(translate_stream_event(event))
        assert out and out[0].kind == "text_delta" and out[0].text == "delta"

    def test_on_chain_stream_non_text_chunk_is_ignored(self):
        """Chain-level chunks that aren't text (graph state dicts,
        Pydantic models, ...) must not leak into the TTS stream."""
        event = {
            "event": "on_chain_stream",
            "name": "StateGraph",
            "run_id": "c1",
            "data": {"chunk": {"counter": 7}},
        }
        out = list(translate_stream_event(event))
        assert out == []

    def test_on_chain_stream_dict_output_key_is_spoken(self):
        """A conventional ``{"output": "..."}`` chain-result dict
        (``AgentExecutor`` / ``return_direct`` tool) must surface its
        string answer instead of being treated as structured-only."""
        event = {
            "event": "on_chain_stream",
            "name": "AgentExecutor",
            "run_id": "c1",
            "data": {"chunk": {"output": "the answer"}},
        }
        out = list(translate_stream_event(event))
        assert out and out[0].kind == "text_delta" and out[0].text == "the answer"

    def test_on_chain_stream_dict_non_string_output_is_ignored(self):
        """A structured payload under a conventional key (``{"answer":
        42}``, ``with_structured_output(...)``) is still kept out of the
        audio stream — only string values are spoken."""
        event = {
            "event": "on_chain_stream",
            "name": "RunnableLambda",
            "run_id": "c1",
            "data": {"chunk": {"answer": 42, "sources": ["a", "b"]}},
        }
        assert list(translate_stream_event(event)) == []

    def test_nested_chain_streams_dedupe_to_root_run(self):
        """``RunnableLambda(f) | RunnableLambda(g)`` (no model
        descendant) emits ``on_chain_stream`` for each child *and* for
        the parent that forwards the composed result.  Only the root
        run's stream is the final answer; child streams would
        double-speak intermediate values (``"a"``, ``"ab"``, ``"ab"``)."""
        state: dict[str, Any] = {}
        events = [
            {
                "event": "on_chain_start",
                "name": "RunnableSequence",
                "run_id": "seq",
                "parent_ids": [],
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
                "event": "on_chain_stream",
                "name": "RunnableLambda",
                "run_id": "g",
                "parent_ids": ["seq"],
                "data": {"chunk": "ab"},
            },
            {
                "event": "on_chain_stream",
                "name": "RunnableSequence",
                "run_id": "seq",
                "parent_ids": [],
                "data": {"chunk": "ab"},
            },
        ]
        out: list[Any] = []
        for ev in events:
            out.extend(translate_stream_event(ev, state=state))
        assert [e.text for e in out if e.kind == "text_delta"] == ["ab"]

    def test_on_chain_stream_emits_without_state(self):
        """The standalone-translator contract: a bare call with no
        ``state`` keeps emitting every chain chunk (the dedupe only
        engages once the bridge threads root-run bookkeeping)."""
        event = {
            "event": "on_chain_stream",
            "name": "RunnableLambda",
            "run_id": "child",
            "parent_ids": ["seq"],
            "data": {"chunk": "hi"},
        }
        out = list(translate_stream_event(event))
        assert [e.text for e in out] == ["hi"]

    def test_on_llm_stream_generation_chunk_yields_text_delta(self):
        """Non-chat ``BaseLLM`` runnables (text-completion models,
        ``FakeStreamingListLLM``) emit ``on_llm_stream`` with a
        ``GenerationChunk``-like payload whose token text lives on
        ``.text``.  Without an explicit handler the bridge suppresses
        the parent chain's chunks (to dedupe chat-model streams) but
        the LLM's text would otherwise be silently dropped."""

        class _GenerationChunk:
            def __init__(self, text: str) -> None:
                self.text = text

        event = {
            "event": "on_llm_stream",
            "name": "FakeStreamingListLLM",
            "run_id": "l1",
            "data": {"chunk": _GenerationChunk("hello")},
        }
        out = list(translate_stream_event(event))
        assert len(out) == 1
        assert out[0].kind == "text_delta"
        assert out[0].text == "hello"

    def test_on_llm_stream_string_chunk_yields_text_delta(self):
        """Some duck-typed providers stream a bare string."""
        event = {
            "event": "on_llm_stream",
            "name": "CustomLLM",
            "run_id": "l1",
            "data": {"chunk": " world"},
        }
        out = list(translate_stream_event(event))
        assert out and out[0].text == " world"

    def test_on_llm_end_emits_text_for_non_streaming_llm(self):
        """``FakeStreamingListLLM`` (and similar non-streaming
        ``BaseLLM`` subclasses) emit only ``on_llm_end`` carrying an
        ``LLMResult`` dict.  The translator must surface its
        ``generations[0][0]["text"]`` so the LLM's response isn't lost
        when the bridge suppresses the parent chain's chunks."""
        event = {
            "event": "on_llm_end",
            "name": "FakeStreamingListLLM",
            "run_id": "l1",
            "data": {
                "output": {
                    "generations": [[{"text": "hello world", "type": "Generation"}]],
                    "llm_output": None,
                }
            },
        }
        out = list(translate_stream_event(event))
        assert len(out) == 1
        assert out[0].kind == "text_delta"
        assert out[0].text == "hello world"

    def test_on_llm_end_skipped_after_streaming(self):
        """Real streaming LLMs emit ``on_llm_stream`` deltas *and* a
        terminal ``on_llm_end`` carrying the full text — emitting the
        end-of-LLM text would double the response on top of the
        already-translated stream chunks.  Translator must dedupe by
        ``run_id``."""

        class _GenerationChunk:
            def __init__(self, text: str) -> None:
                self.text = text

        state: dict[str, Any] = {}
        stream_event = {
            "event": "on_llm_stream",
            "name": "OpenAI",
            "run_id": "l1",
            "data": {"chunk": _GenerationChunk("hi ")},
        }
        end_event = {
            "event": "on_llm_end",
            "name": "OpenAI",
            "run_id": "l1",
            "data": {"output": {"generations": [[{"text": "hi there", "type": "Generation"}]]}},
        }
        stream_out = list(translate_stream_event(stream_event, state=state))
        end_out = list(translate_stream_event(end_event, state=state))
        assert [e.text for e in stream_out] == ["hi "]
        # End event must NOT re-emit text — the stream already covered it.
        assert end_out == []

    def test_on_chat_model_end_emits_text_for_non_streaming_chat_model(self):
        """Non-streaming chat models (any chat model that doesn't override
        ``_stream`` / ``_astream``) only surface their AIMessage via
        ``on_chat_model_end`` — no ``on_chat_model_stream`` events fire and
        the parent chain's stream chunks carrying the same message are
        suppressed by ``chains_with_chat_model_descendants``.  Without the
        ``on_chat_model_end`` fallback the assistant goes silent."""
        state: dict[str, Any] = {}
        start_event = {
            "event": "on_chat_model_start",
            "name": "ChatOpenAI",
            "run_id": "m1",
            "parent_ids": ["seq"],
            "data": {},
        }
        end_event = {
            "event": "on_chat_model_end",
            "name": "ChatOpenAI",
            "run_id": "m1",
            "parent_ids": ["seq"],
            "data": {"output": _MockAIMessageChunk(content="hello world")},
        }
        list(translate_stream_event(start_event, state=state))
        end_out = list(translate_stream_event(end_event, state=state))
        assert [e.kind for e in end_out] == ["text_delta"]
        assert end_out[0].text == "hello world"

    def test_on_chat_model_end_skipped_after_streaming(self):
        """Streaming chat models emit ``on_chat_model_stream`` deltas
        plus a terminal ``on_chat_model_end`` carrying the full message.
        The end-of-model fallback must dedupe by ``run_id`` so the
        response isn't doubled on top of the already-streamed tokens."""
        state: dict[str, Any] = {}
        stream_event = {
            "event": "on_chat_model_stream",
            "name": "ChatOpenAI",
            "run_id": "m1",
            "parent_ids": ["seq"],
            "data": {"chunk": _MockAIMessageChunk(content="hi ")},
        }
        end_event = {
            "event": "on_chat_model_end",
            "name": "ChatOpenAI",
            "run_id": "m1",
            "parent_ids": ["seq"],
            "data": {"output": _MockAIMessageChunk(content="hi there")},
        }
        stream_out = list(translate_stream_event(stream_event, state=state))
        end_out = list(translate_stream_event(end_event, state=state))
        assert [e.text for e in stream_out] == ["hi "]
        assert end_out == []

    def test_same_name_parallel_tool_calls_preserve_ids_fifo(self):
        """When the model fires the same tool more than once in one
        response, each ``on_tool_start`` must match the *next* queued
        provider call-id rather than the last-seen one, otherwise the
        first tool_started/tool_result pair is misrouted and the count
        of ``tool_started`` vs ``tool_result`` events drifts."""
        # Two parallel "search" calls in a single chat-model chunk.
        chunk = _MockAIMessageChunk(
            content="",
            tool_call_chunks=[
                {"name": "search", "args": None, "id": "call-a", "index": 0},
                {"name": "search", "args": None, "id": "call-b", "index": 1},
            ],
        )
        chunk_event = {
            "event": "on_chat_model_stream",
            "name": "ChatOpenAI",
            "run_id": "m1",
            "data": {"chunk": chunk},
        }
        state: dict[str, Any] = {}
        chunk_out = list(translate_stream_event(chunk_event, state=state))
        # Both started events come from the chat_model chunk path; the
        # framework's on_tool_start events that follow must dedupe.
        started_chunks = [e for e in chunk_out if e.kind == "tool_started"]
        assert [e.call_id for e in started_chunks] == ["call-a", "call-b"]

        start_a = {
            "event": "on_tool_start",
            "name": "search",
            "run_id": "tool-run-a",
            "data": {"input": {"q": "first"}},
        }
        start_b = {
            "event": "on_tool_start",
            "name": "search",
            "run_id": "tool-run-b",
            "data": {"input": {"q": "second"}},
        }
        out_a = list(translate_stream_event(start_a, state=state))
        out_b = list(translate_stream_event(start_b, state=state))
        # Framework starts must be suppressed (chunk path already
        # announced both calls); same-name parallel calls would
        # otherwise leak duplicate started events with run_ids.
        assert out_a == []
        assert out_b == []

        end_a = {
            "event": "on_tool_end",
            "name": "search",
            "run_id": "tool-run-a",
            "data": {"output": "result-a"},
        }
        end_b = {
            "event": "on_tool_end",
            "name": "search",
            "run_id": "tool-run-b",
            "data": {"output": "result-b"},
        }
        result_a = list(translate_stream_event(end_a, state=state))
        result_b = list(translate_stream_event(end_b, state=state))
        # FIFO mapping: first on_tool_start was paired with the first
        # queued chunk id (call-a), so its on_tool_end must surface
        # call-a — not call-b.
        assert [(e.kind, e.call_id) for e in result_a] == [("tool_result", "call-a")]
        assert [(e.kind, e.call_id) for e in result_b] == [("tool_result", "call-b")]

    def test_chunk_text_prefers_text_property(self):
        """``AIMessageChunk.text`` flattens ``content_blocks`` across
        providers (Anthropic ``thinking``, OpenAI ``reasoning``
        summaries).  When a chunk exposes ``.text``, the translator
        should use it directly instead of walking raw ``content``."""

        class _ChunkWithText:
            text = "flat text from blocks"
            content: object = [
                {"type": "thinking", "thinking": "private"},
                {"type": "text", "text": "raw fallback"},
            ]
            tool_call_chunks: list[Any] = []  # noqa: RUF012 test fake uses shared class fixture

        event = {
            "event": "on_chat_model_stream",
            "name": "ChatAnthropic",
            "run_id": "r1",
            "data": {"chunk": _ChunkWithText()},
        }
        out = list(translate_stream_event(event))
        assert out[0].text == "flat text from blocks"

    def test_on_custom_event_text_payload_yields_text_delta(self):
        """LCEL ``dispatch_custom_event`` calls surface as
        ``on_custom_event``; payloads that carry a ``"text"``/``"speak"``
        field should drive TTS."""
        event = {
            "event": "on_custom_event",
            "name": "status",
            "run_id": "c1",
            "data": {"text": "looking that up..."},
        }
        out = list(translate_stream_event(event))
        assert len(out) == 1
        assert out[0].kind == "text_delta"
        assert out[0].text == "looking that up..."

    def test_on_custom_event_string_payload_is_silent(self):
        """Bare strings are common for debug/progress telemetry and
        must not be spoken unless wrapped in an explicit speech field."""
        event = {
            "event": "on_custom_event",
            "name": "status",
            "run_id": "c1",
            "data": "plain progress string",
        }
        out = list(translate_stream_event(event))
        assert out == []

    def test_on_custom_event_telemetry_payload_is_silent(self):
        """Custom events that carry only opaque telemetry (no
        ``text``/``speak`` field) must not leak into TTS."""
        event = {
            "event": "on_custom_event",
            "name": "progress",
            "run_id": "c1",
            "data": {"progress": 0.5, "step": 3},
        }
        out = list(translate_stream_event(event))
        assert out == []
