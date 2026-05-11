"""Tests for :class:`LangChainBridge`.

Uses duck-typed mock objects so the real ``langchain-core`` package is
not required at test time.  The translator module and bridge both rely
only on attribute access, so tests mirror the event shapes described in
``plan/peripheral-langchain-langgraph-bridge.md`` and the real
LangChain ``astream_events(version="v2")`` contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents._langchain_events import translate_stream_event
from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import (
    AgentTurnInput,
    BridgeInputError,
    CancellationMode,
    CommitRule,
    RecorderContext,
    UnitKind,
)
from easycat.integrations.agents.langchain import LangChainBridge
from easycat.runtime.journal import InMemoryRingBuffer

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
