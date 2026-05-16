"""Tests for :class:`LangChainBridge`.

Uses duck-typed mock objects so the real ``langchain-core`` package is
not required at test time.  The translator module and bridge both rely
only on attribute access, so tests mirror the event shapes described in
``plan/peripheral-langchain-langgraph-bridge.md`` and the real
LangChain ``astream_events(version="v2")`` contract.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents._agent_runner import AgentRunner, AgentRunnerConfig
from easycat.integrations.agents._langchain_events import translate_stream_event
from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import (
    NULL_RECORDER,
    AgentTurnInput,
    BridgeInputError,
    CancellationMode,
    CommitRule,
    RecorderContext,
    UnitKind,
)
from easycat.integrations.agents.langchain import LangChainBridge
from easycat.runtime.journal import InMemoryRingBuffer
from easycat.timeouts import AgentTimeoutError

# ── Mock LangChain objects ───────────────────────────────────────


class _MockAIMessageChunk:
    """Duck-types as ``langchain_core.messages.AIMessageChunk``."""

    def __init__(
        self,
        content: str = "",
        tool_call_chunks: list[dict[str, Any]] | None = None,
    ) -> None:
        self.content = content
        self.tool_call_chunks = tool_call_chunks or []
        self.id = "chunk-id"


class _MockRunnable:
    """Duck-types as ``langchain_core.runnables.Runnable``.

    Only implements the subset ``LangChainBridge`` relies on:
    ``astream_events(input, version=...)``.
    """

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events
        self.invoked_with: Any = None

    async def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        self.invoked_with = (input, kwargs)
        for event in self._events:
            yield event

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any: ...


def _recorder(journal: InMemoryRingBuffer | None = None) -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


def _content_of_history_item(item: Any) -> Any:
    """Tolerate both dict-shaped and typed-message history items."""
    if isinstance(item, dict):
        return item.get("content")
    return getattr(item, "content", None)


# ── Translator tests ─────────────────────────────────────────────


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
        suppressed by ``chains_with_model_descendants``.  Without the
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
            tool_call_chunks: list[Any] = []

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

    def test_on_custom_event_string_payload_yields_text_delta(self):
        event = {
            "event": "on_custom_event",
            "name": "status",
            "run_id": "c1",
            "data": "plain progress string",
        }
        out = list(translate_stream_event(event))
        assert out and out[0].kind == "text_delta"
        assert out[0].text == "plain progress string"

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


# ── LangChainBridge tests ────────────────────────────────────────


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
                await asyncio.sleep(999)
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
    async def test_chain_wrapping_text_llm_streams_text(self):
        """Chains like ``PromptTemplate | FakeStreamingListLLM`` use a
        non-chat ``BaseLLM`` whose tokens surface via ``on_llm_stream``
        rather than ``on_chat_model_stream``.  The bridge marks the
        parent chain as having a model descendant — so its forwarded
        ``on_chain_stream`` chunks are suppressed — meaning the LLM's
        own stream events must be translated or the ``done`` event ends
        up empty."""

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
        bridge suppresses chain chunks once an LLM descendant is
        observed (to dedupe real streaming), so without translating
        ``on_llm_end`` the LLM's text would be silently dropped."""
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
        # Exactly one emission from on_llm_end — chain chunks suppressed.
        assert text == "hello world"
        assert done and done[0].text == "hello world"

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
        ai_msgs = [m for m in bridge._message_history if getattr(m, "type", None) == "ai"]
        assert ai_msgs and ai_msgs[-1].content == "ABC"

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


class TestLangChainBridgeSnapshot:
    def test_snapshot_state_kind(self):
        runnable = _MockRunnable([])
        bridge = LangChainBridge(runnable, display_name="MyChain")
        snap = bridge.snapshot_state()
        assert snap.kind == "langchain"
        assert snap.fields["runnable"] == "MyChain"
        assert snap.fields["history_length"] == 0


def _role_of_msg(item: Any) -> Any:
    """Tolerate dict-shaped and typed-message items (role accessor)."""
    if isinstance(item, dict):
        return item.get("role")
    return getattr(item, "type", None)  # langchain messages expose ``.type``


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


# ── Partial-turn preservation on cancellation ────────────────────


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
                await asyncio.sleep(999)

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


# ── RunnableWithMessageHistory store sync ────────────────────────


class _InMemoryStore:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def add_message(self, m: Any) -> None:
        self.messages.append(m)

    def add_messages(self, ms: list[Any]) -> None:
        self.messages.extend(ms)

    def clear(self) -> None:
        self.messages.clear()


class _FakeHistoryRunnable:
    """Duck-types ``RunnableWithMessageHistory``: each turn persists the
    user input + model output into a per-session store and (the real
    wrapper) rebuilds the prompt history from it, *overwriting* the
    bridge's ``history`` key.  So shadow-list-only edits are invisible
    to the next turn unless mirrored into this store."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.history_factory_config: list[Any] = []
        self._stores: dict[str, _InMemoryStore] = {}

    def get_session_history(self, session_id: str) -> _InMemoryStore:
        return self._stores.setdefault(session_id, _InMemoryStore())

    async def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        sid = kwargs["config"]["configurable"]["session_id"]
        store = self.get_session_history(sid)
        store.add_message({"role": "user", "content": "q"})
        yield {
            "event": "on_chat_model_stream",
            "name": "ChatOpenAI",
            "run_id": "m",
            "parent_ids": [],
            "data": {"chunk": _MockAIMessageChunk(content=self._reply)},
        }
        store.add_message({"role": "assistant", "content": self._reply})

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any: ...


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
