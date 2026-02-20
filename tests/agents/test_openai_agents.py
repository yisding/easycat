"""Tests for OpenAIAgentsAdapter.

Uses lightweight mock objects that replicate the OpenAI Agents SDK
(``openai-agents``) API surface so the tests run without the package
installed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from easycat.agent_runner import AgentStreamEventType
from easycat.agents.openai_agents import (
    OpenAIAgentsAdapter,
    _extract_text_delta,
    _extract_tool_delta,
    _map_run_item_event,
)
from easycat.cancel import CancelToken

# ── Mock OpenAI Agents SDK objects ────────────────────────────────


@dataclass
class MockResponseTextDeltaEvent:
    """Mimics openai.types.responses.ResponseTextDeltaEvent."""

    type: str = "response.output_text.delta"
    delta: str = ""


@dataclass
class MockOtherResponseEvent:
    """A non-text response event (e.g., response.created)."""

    type: str = "response.created"


@dataclass
class MockFunctionCallArgsDeltaEvent:
    """Mimics ResponseFunctionCallArgumentsDeltaEvent."""

    type: str = "response.function_call_arguments.delta"
    delta: str = ""
    call_id: str = ""
    item_id: str = ""


@dataclass
class MockRawItem:
    """Mimics the raw_item on a ToolCallItem / ToolCallOutputItem."""

    name: str = ""
    call_id: str = ""


@dataclass
class MockToolCallItem:
    type: str = "tool_call_item"
    raw_item: MockRawItem | None = None


@dataclass
class MockToolCallOutputItem:
    type: str = "tool_call_output_item"
    raw_item: MockRawItem | None = None
    output: str = ""


@dataclass
class MockMessageOutputItem:
    type: str = "message_output_item"


@dataclass
class MockStreamEvent:
    """Mimics a StreamEvent from result.stream_events()."""

    type: str = ""
    data: Any = None
    item: Any = None


class MockRunResult:
    """Mimics agents.RunResult."""

    def __init__(self, final_output: str, input_list: list[Any] | None = None) -> None:
        self.final_output = final_output
        self._input_list = input_list or []

    def to_input_list(self) -> list[Any]:
        return list(self._input_list)


class MockRunResultStreaming:
    """Mimics agents.RunResultStreaming."""

    def __init__(
        self,
        events: list[MockStreamEvent],
        input_list: list[Any] | None = None,
    ) -> None:
        self._events = events
        self._input_list = input_list or []

    async def stream_events(self) -> AsyncIterator[MockStreamEvent]:
        for event in self._events:
            yield event

    def to_input_list(self) -> list[Any]:
        return list(self._input_list)


class MockSlowRunResultStreaming(MockRunResultStreaming):
    """Streaming result with delays between events (for cancellation tests)."""

    async def stream_events(self) -> AsyncIterator[MockStreamEvent]:
        for event in self._events:
            await asyncio.sleep(0.05)
            yield event


class MockRunner:
    """Mimics agents.Runner with captured calls."""

    def __init__(
        self,
        run_results: list[MockRunResult] | None = None,
        stream_results: list[MockRunResultStreaming] | None = None,
    ) -> None:
        self._run_results = list(run_results or [])
        self._stream_results = list(stream_results or [])
        self._run_call_count = 0
        self._stream_call_count = 0
        self.run_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def run(self, agent: Any, input_data: Any, **kwargs: Any) -> MockRunResult:
        self.run_calls.append({"agent": agent, "input": input_data, **kwargs})
        idx = min(self._run_call_count, len(self._run_results) - 1)
        self._run_call_count += 1
        return self._run_results[idx]

    async def run_streamed(
        self, agent: Any, input_data: Any, **kwargs: Any
    ) -> MockRunResultStreaming:
        self.stream_calls.append({"agent": agent, "input": input_data, **kwargs})
        idx = min(self._stream_call_count, len(self._stream_results) - 1)
        self._stream_call_count += 1
        return self._stream_results[idx]


class MockAgent:
    """Mimics agents.Agent — just a config object."""

    def __init__(self, name: str = "test") -> None:
        self.name = name


# ── Fixtures ──────────────────────────────────────────────────────


def _make_text_events(chunks: list[str]) -> list[MockStreamEvent]:
    """Build a list of raw_response_event stream events from text chunks."""
    return [
        MockStreamEvent(
            type="raw_response_event",
            data=MockResponseTextDeltaEvent(delta=chunk),
        )
        for chunk in chunks
    ]


def _make_tool_events() -> list[MockStreamEvent]:
    """Build tool_call + tool_call_output stream events."""
    return [
        MockStreamEvent(
            type="run_item_stream_event",
            item=MockToolCallItem(
                raw_item=MockRawItem(name="get_weather", call_id="call_abc"),
            ),
        ),
        MockStreamEvent(
            type="run_item_stream_event",
            item=MockToolCallOutputItem(
                raw_item=MockRawItem(call_id="call_abc"),
                output="sunny, 72°F",
            ),
        ),
    ]


def _make_tool_delta_events(chunks: list[str], call_id: str = "call_abc") -> list[MockStreamEvent]:
    """Build raw_response_event stream events for function call argument deltas."""
    return [
        MockStreamEvent(
            type="raw_response_event",
            data=MockFunctionCallArgsDeltaEvent(delta=chunk, call_id=call_id),
        )
        for chunk in chunks
    ]


def _input_list_for(prompt: str, response: str) -> list[dict[str, str]]:
    return [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": response},
    ]


# ── Helper unit tests ────────────────────────────────────────────


class TestExtractTextDelta:
    def test_extracts_delta(self):
        event = MockResponseTextDeltaEvent(delta="Hello")
        assert _extract_text_delta(event) == "Hello"

    def test_returns_empty_for_non_text_event(self):
        event = MockOtherResponseEvent()
        assert _extract_text_delta(event) == ""

    def test_returns_empty_for_none_delta(self):
        event = MockResponseTextDeltaEvent(delta=None)  # type: ignore[arg-type]
        assert _extract_text_delta(event) == ""


class TestMapRunItemEvent:
    def test_maps_tool_call(self):
        item = MockToolCallItem(raw_item=MockRawItem(name="search", call_id="c1"))
        result = _map_run_item_event(item)
        assert result is not None
        assert result.type == AgentStreamEventType.TOOL_STARTED
        assert result.tool_name == "search"
        assert result.call_id == "c1"

    def test_maps_tool_output(self):
        item = MockToolCallOutputItem(
            raw_item=MockRawItem(call_id="c1"),
            output="found it",
        )
        result = _map_run_item_event(item)
        assert result is not None
        assert result.type == AgentStreamEventType.TOOL_RESULT
        assert result.call_id == "c1"
        assert result.result == "found it"

    def test_skips_message_output(self):
        item = MockMessageOutputItem()
        assert _map_run_item_event(item) is None

    def test_skips_unknown_type(self):
        item = type("Unknown", (), {"type": "unknown_item"})()
        assert _map_run_item_event(item) is None


# ── Basic run() tests ─────────────────────────────────────────────


@pytest.fixture
def mock_runner():
    return MockRunner()


@pytest.fixture
def mock_agent():
    return MockAgent(name="TestBot")


@pytest.mark.asyncio
async def test_run_returns_response(monkeypatch):
    runner = MockRunner(run_results=[MockRunResult("Hello there!")])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    result = await adapter.run("Hi")
    assert result == "Hello there!"


@pytest.mark.asyncio
async def test_run_passes_input(monkeypatch):
    runner = MockRunner(run_results=[MockRunResult("ok", _input_list_for("What?", "ok"))])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    agent = MockAgent()
    adapter = OpenAIAgentsAdapter(agent)
    await adapter.run("What?")
    assert runner.run_calls[0]["input"] == "What?"
    assert runner.run_calls[0]["agent"] is agent


@pytest.mark.asyncio
async def test_run_passes_run_config(monkeypatch):
    runner = MockRunner(run_results=[MockRunResult("ok")])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    config = {"model": "gpt-5.2"}
    adapter = OpenAIAgentsAdapter(MockAgent(), run_config=config)
    await adapter.run("test")
    assert runner.run_calls[0]["run_config"] == config


@pytest.mark.asyncio
async def test_run_passes_context(monkeypatch):
    runner = MockRunner(run_results=[MockRunResult("ok")])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    ctx = {"user_id": "123"}
    adapter = OpenAIAgentsAdapter(MockAgent(), context=ctx)
    await adapter.run("test")
    assert runner.run_calls[0]["context"] == ctx


# ── Multi-turn history tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_run_tracks_message_history(monkeypatch):
    history1 = _input_list_for("turn 1", "reply 1")
    history2 = history1 + _input_list_for("turn 2", "reply 2")
    runner = MockRunner(
        run_results=[
            MockRunResult("reply 1", history1),
            MockRunResult("reply 2", history2),
        ]
    )
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())

    await adapter.run("turn 1")
    assert len(adapter.message_history) == 2

    await adapter.run("turn 2")
    # Second call should include history from first turn + new user message
    second_input = runner.run_calls[1]["input"]
    assert isinstance(second_input, list)
    assert len(second_input) == 3  # 2 from history + 1 new user message


@pytest.mark.asyncio
async def test_run_first_call_string_input(monkeypatch):
    runner = MockRunner(run_results=[MockRunResult("ok")])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    await adapter.run("hello")
    # First call should pass plain string (no history)
    assert runner.run_calls[0]["input"] == "hello"


@pytest.mark.asyncio
async def test_clear_history(monkeypatch):
    runner = MockRunner(
        run_results=[
            MockRunResult("r1", _input_list_for("t1", "r1")),
            MockRunResult("r2"),
        ]
    )
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    await adapter.run("t1")
    assert len(adapter.message_history) > 0

    adapter.clear_history()
    assert adapter.message_history == []

    await adapter.run("fresh start")
    # After clearing, should pass plain string again
    assert runner.run_calls[1]["input"] == "fresh start"


# ── Streaming run_streaming() tests ──────────────────────────────


@pytest.mark.asyncio
async def test_streaming_yields_text_deltas(monkeypatch):
    events = _make_text_events(["Hello", " world", "!"])
    runner = MockRunner(stream_results=[MockRunResultStreaming(events)])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    collected = []
    async for event in adapter.run_streaming("greet"):
        collected.append(event)

    text_deltas = [e for e in collected if e.type == AgentStreamEventType.TEXT_DELTA]
    assert len(text_deltas) == 3
    assert text_deltas[0].text == "Hello"
    assert text_deltas[1].text == " world"
    assert text_deltas[2].text == "!"


@pytest.mark.asyncio
async def test_streaming_yields_done_event(monkeypatch):
    events = _make_text_events(["Hi", " there"])
    runner = MockRunner(stream_results=[MockRunResultStreaming(events)])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    collected = []
    async for event in adapter.run_streaming("greet"):
        collected.append(event)

    done = [e for e in collected if e.type == AgentStreamEventType.DONE]
    assert len(done) == 1
    assert done[0].text == "Hi there"


@pytest.mark.asyncio
async def test_streaming_yields_tool_events(monkeypatch):
    events = _make_tool_events() + _make_text_events(["The weather is nice."])
    runner = MockRunner(stream_results=[MockRunResultStreaming(events)])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    collected = []
    async for event in adapter.run_streaming("weather"):
        collected.append(event)

    types = [e.type for e in collected]
    assert AgentStreamEventType.TOOL_STARTED in types
    assert AgentStreamEventType.TOOL_RESULT in types
    assert AgentStreamEventType.TEXT_DELTA in types

    tool_started = [e for e in collected if e.type == AgentStreamEventType.TOOL_STARTED][0]
    assert tool_started.tool_name == "get_weather"
    assert tool_started.call_id == "call_abc"

    tool_result = [e for e in collected if e.type == AgentStreamEventType.TOOL_RESULT][0]
    assert tool_result.call_id == "call_abc"
    assert tool_result.result == "sunny, 72°F"


@pytest.mark.asyncio
async def test_streaming_skips_non_text_raw_events(monkeypatch):
    events = [
        MockStreamEvent(type="raw_response_event", data=MockOtherResponseEvent()),
        *_make_text_events(["ok"]),
    ]
    runner = MockRunner(stream_results=[MockRunResultStreaming(events)])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    collected = []
    async for event in adapter.run_streaming("test"):
        collected.append(event)

    text_deltas = [e for e in collected if e.type == AgentStreamEventType.TEXT_DELTA]
    assert len(text_deltas) == 1
    assert text_deltas[0].text == "ok"


@pytest.mark.asyncio
async def test_streaming_updates_message_history(monkeypatch):
    history = _input_list_for("t1", "Hello world")
    events1 = _make_text_events(["Hello", " world"])
    events2 = _make_text_events(["Second"])
    runner = MockRunner(
        stream_results=[
            MockRunResultStreaming(events1, input_list=history),
            MockRunResultStreaming(events2, input_list=history + _input_list_for("t2", "Second")),
        ]
    )
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    async for _ in adapter.run_streaming("t1"):
        pass
    assert len(adapter.message_history) == 2

    async for _ in adapter.run_streaming("t2"):
        pass
    # Second call should include history from first turn + new user message
    second_input = runner.stream_calls[1]["input"]
    assert isinstance(second_input, list)


# ── Cancellation tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_respects_cancel_token(monkeypatch):
    events = _make_text_events(["Hello", " world", " how", " are", " you"])
    runner = MockRunner(stream_results=[MockSlowRunResultStreaming(events)])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    token = CancelToken()

    collected = []
    async for event in adapter.run_streaming("test", cancel_token=token):
        collected.append(event)
        if event.type == AgentStreamEventType.TEXT_DELTA:
            token.cancel()

    text_deltas = [e for e in collected if e.type == AgentStreamEventType.TEXT_DELTA]
    assert len(text_deltas) == 1
    assert text_deltas[0].text == "Hello"


@pytest.mark.asyncio
async def test_streaming_cancel_still_emits_done(monkeypatch):
    events = _make_text_events(["Hello", " world"])
    runner = MockRunner(stream_results=[MockSlowRunResultStreaming(events)])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    token = CancelToken()

    collected = []
    async for event in adapter.run_streaming("test", cancel_token=token):
        collected.append(event)
        if event.type == AgentStreamEventType.TEXT_DELTA:
            token.cancel()

    done = [e for e in collected if e.type == AgentStreamEventType.DONE]
    assert len(done) == 1


# ── Context parameter is accepted ────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_accepts_context_param(monkeypatch):
    events = _make_text_events(["ok"])
    runner = MockRunner(stream_results=[MockRunResultStreaming(events)])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    collected = []
    async for event in adapter.run_streaming(
        "test",
        context=[{"role": "user", "content": "prior"}],
    ):
        collected.append(event)

    assert any(e.type == AgentStreamEventType.DONE for e in collected)


# ── Protocol compatibility tests ─────────────────────────────────


def test_adapter_has_run_streaming():
    adapter = OpenAIAgentsAdapter(MockAgent())
    assert hasattr(adapter, "run_streaming")
    assert callable(adapter.run_streaming)


def test_adapter_has_clear_history():
    adapter = OpenAIAgentsAdapter(MockAgent())
    assert hasattr(adapter, "clear_history")
    assert callable(adapter.clear_history)


def test_adapter_has_run():
    adapter = OpenAIAgentsAdapter(MockAgent())
    assert hasattr(adapter, "run")
    assert callable(adapter.run)


# ── Base class inheritance ────────────────────────────────────────


def test_inherits_from_base():
    from easycat.agents.base import BaseAgentAdapter

    adapter = OpenAIAgentsAdapter(MockAgent())
    assert isinstance(adapter, BaseAgentAdapter)


# ── _extract_tool_delta unit tests ────────────────────────────────


class TestExtractToolDelta:
    def test_extracts_delta(self):
        event = MockFunctionCallArgsDeltaEvent(delta='{"city":', call_id="call_1")
        result = _extract_tool_delta(event)
        assert result is not None
        assert result.type == AgentStreamEventType.TOOL_DELTA
        assert result.text == '{"city":'
        assert result.call_id == "call_1"

    def test_uses_item_id_fallback(self):
        """When call_id is empty, falls back to item_id."""
        event = MockFunctionCallArgsDeltaEvent(delta="args", call_id="", item_id="item_42")
        result = _extract_tool_delta(event)
        assert result is not None
        assert result.call_id == "item_42"

    def test_returns_none_for_non_tool_event(self):
        event = MockOtherResponseEvent()
        assert _extract_tool_delta(event) is None

    def test_returns_none_for_text_delta_event(self):
        event = MockResponseTextDeltaEvent(delta="Hello")
        assert _extract_tool_delta(event) is None

    def test_returns_none_for_empty_delta(self):
        event = MockFunctionCallArgsDeltaEvent(delta="", call_id="call_1")
        assert _extract_tool_delta(event) is None


# ── TOOL_DELTA streaming tests ───────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_yields_tool_delta_events(monkeypatch):
    """Tool call argument deltas should yield TOOL_DELTA events."""
    tool_start = [
        MockStreamEvent(
            type="run_item_stream_event",
            item=MockToolCallItem(
                raw_item=MockRawItem(name="get_weather", call_id="call_abc"),
            ),
        ),
    ]
    tool_deltas = _make_tool_delta_events(['{"city":', ' "London"}'], call_id="call_abc")
    tool_end = [
        MockStreamEvent(
            type="run_item_stream_event",
            item=MockToolCallOutputItem(
                raw_item=MockRawItem(call_id="call_abc"),
                output="sunny, 72°F",
            ),
        ),
    ]
    text = _make_text_events(["The weather is nice."])

    all_events = tool_start + tool_deltas + tool_end + text
    runner = MockRunner(stream_results=[MockRunResultStreaming(all_events)])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    collected = []
    async for event in adapter.run_streaming("weather"):
        collected.append(event)

    types = [e.type for e in collected]
    assert AgentStreamEventType.TOOL_STARTED in types
    assert AgentStreamEventType.TOOL_DELTA in types
    assert AgentStreamEventType.TOOL_RESULT in types
    assert AgentStreamEventType.TEXT_DELTA in types
    assert AgentStreamEventType.DONE in types

    tool_delta_events = [e for e in collected if e.type == AgentStreamEventType.TOOL_DELTA]
    assert len(tool_delta_events) == 2
    assert tool_delta_events[0].text == '{"city":'
    assert tool_delta_events[0].call_id == "call_abc"
    assert tool_delta_events[1].text == ' "London"}'


@pytest.mark.asyncio
async def test_streaming_full_tool_lifecycle(monkeypatch):
    """Full lifecycle: TOOL_STARTED → TOOL_DELTA(s) → TOOL_RESULT → TEXT_DELTA → DONE."""
    events = [
        # Tool started
        MockStreamEvent(
            type="run_item_stream_event",
            item=MockToolCallItem(
                raw_item=MockRawItem(name="search", call_id="c1"),
            ),
        ),
        # Tool argument deltas
        MockStreamEvent(
            type="raw_response_event",
            data=MockFunctionCallArgsDeltaEvent(delta='{"q":', call_id="c1"),
        ),
        MockStreamEvent(
            type="raw_response_event",
            data=MockFunctionCallArgsDeltaEvent(delta=' "test"}', call_id="c1"),
        ),
        # Tool result
        MockStreamEvent(
            type="run_item_stream_event",
            item=MockToolCallOutputItem(
                raw_item=MockRawItem(call_id="c1"),
                output="3 results found",
            ),
        ),
        # Text response
        *_make_text_events(["Here are the results."]),
    ]
    runner = MockRunner(stream_results=[MockRunResultStreaming(events)])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    collected = []
    async for event in adapter.run_streaming("search"):
        collected.append(event)

    # Verify event order: TOOL_STARTED, TOOL_DELTA, TOOL_DELTA, TOOL_RESULT, TEXT_DELTA, DONE
    event_types = [e.type for e in collected]
    assert event_types == [
        AgentStreamEventType.TOOL_STARTED,
        AgentStreamEventType.TOOL_DELTA,
        AgentStreamEventType.TOOL_DELTA,
        AgentStreamEventType.TOOL_RESULT,
        AgentStreamEventType.TEXT_DELTA,
        AgentStreamEventType.DONE,
    ]


@pytest.mark.asyncio
async def test_streaming_tool_deltas_without_text(monkeypatch):
    """Tool deltas should not interfere with text accumulation in DONE."""
    events = [
        *_make_tool_delta_events(['{"a":1}'], call_id="c1"),
        *_make_text_events(["Result"]),
    ]
    runner = MockRunner(stream_results=[MockRunResultStreaming(events)])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    collected = []
    async for event in adapter.run_streaming("test"):
        collected.append(event)

    done = [e for e in collected if e.type == AgentStreamEventType.DONE]
    assert done[0].text == "Result"  # Only text deltas contribute to accumulated text


# ── Structured output tests ──────────────────────────────────────


class FakeOpenAIPydanticModel:
    """Fake Pydantic v2 model for testing structured output."""

    def __init__(self, action: str, reasoning: str) -> None:
        self.action = action
        self.reasoning = reasoning

    def model_dump_json(self) -> str:
        return f'{{"action":"{self.action}","reasoning":"{self.reasoning}"}}'


@pytest.mark.asyncio
async def test_run_serializes_structured_output(monkeypatch):
    """run() should use serialize_output for Pydantic models."""
    model = FakeOpenAIPydanticModel("greet", "user said hello")
    runner = MockRunner(run_results=[MockRunResult(final_output=model)])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    result = await adapter.run("test")
    assert result == '{"action":"greet","reasoning":"user said hello"}'
    assert adapter.last_output is model


@pytest.mark.asyncio
async def test_run_stores_last_output(monkeypatch):
    """run() should store the raw output in last_output."""
    runner = MockRunner(run_results=[MockRunResult(final_output="hello")])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    await adapter.run("test")
    assert adapter.last_output == "hello"


@pytest.mark.asyncio
async def test_run_serializes_dict(monkeypatch):
    """run() should serialize dict output as JSON."""
    runner = MockRunner(run_results=[MockRunResult(final_output={"key": "val"})])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    result = await adapter.run("test")
    assert '"key"' in result
    assert '"val"' in result


@pytest.mark.asyncio
async def test_streaming_done_includes_structured_output(monkeypatch):
    """Streaming DONE event should carry structured_output from result.final_output."""

    class MockRunResultStreamingWithOutput(MockRunResultStreaming):
        def __init__(self, events, final_output=None, **kwargs):
            super().__init__(events, **kwargs)
            self.final_output = final_output

    model = FakeOpenAIPydanticModel("search", "user asked to find")
    events = _make_text_events(["Searching..."])
    runner = MockRunner(
        stream_results=[MockRunResultStreamingWithOutput(events, final_output=model)]
    )
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    collected = []
    async for event in adapter.run_streaming("search"):
        collected.append(event)

    done = [e for e in collected if e.type == AgentStreamEventType.DONE]
    assert len(done) == 1
    assert done[0].structured_output is model
    assert adapter.last_output is model


@pytest.mark.asyncio
async def test_streaming_done_structured_output_none_for_text(monkeypatch):
    """For text-only agents, final_output is the text string."""
    events = _make_text_events(["Hello"])
    runner = MockRunner(stream_results=[MockRunResultStreaming(events)])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    collected = []
    async for event in adapter.run_streaming("test"):
        collected.append(event)

    done = [e for e in collected if e.type == AgentStreamEventType.DONE]
    # MockRunResultStreaming doesn't have final_output attr → None
    assert done[0].structured_output is None


@pytest.mark.asyncio
async def test_clear_history_resets_last_output(monkeypatch):
    runner = MockRunner(run_results=[MockRunResult(final_output="reply")])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    await adapter.run("test")
    assert adapter.last_output is not None

    adapter.clear_history()
    assert adapter.last_output is None


# ── Barge-in / interruption tests ─────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_cancel_during_tool_call_lets_tool_complete(monkeypatch):
    """Cancelling mid-tool-call should let the tool result arrive."""
    events = [
        *_make_text_events(["Checking. "]),
        MockStreamEvent(
            type="run_item_stream_event",
            item=MockToolCallItem(
                raw_item=MockRawItem(name="db_update", call_id="c1"),
            ),
        ),
        *_make_tool_delta_events(['{"id": 1}'], call_id="c1"),
        # --- cancel arrives around here ---
        MockStreamEvent(
            type="run_item_stream_event",
            item=MockToolCallOutputItem(
                raw_item=MockRawItem(call_id="c1"),
                output="row updated",
            ),
        ),
        *_make_text_events(["All done."]),
    ]

    input_list = [
        {"role": "user", "content": "update it"},
        {"role": "assistant", "content": "Checking. All done."},
    ]
    result = MockSlowRunResultStreaming(events, input_list=input_list)
    runner = MockRunner(stream_results=[result])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    token = CancelToken()
    collected = []
    async for event in adapter.run_streaming("update it", cancel_token=token):
        collected.append(event)
        # Cancel after seeing the tool started event
        if event.type == AgentStreamEventType.TOOL_STARTED:
            token.cancel()

    types = [e.type for e in collected]

    # Tool events should all be present (tool completed)
    assert AgentStreamEventType.TOOL_STARTED in types
    assert AgentStreamEventType.TOOL_RESULT in types

    # Text before cancellation should be present
    assert AgentStreamEventType.TEXT_DELTA in types
    text_deltas = [e for e in collected if e.type == AgentStreamEventType.TEXT_DELTA]
    assert text_deltas[0].text == "Checking. "
    # "All done." text AFTER tool should NOT be present (cancelled)
    assert len(text_deltas) == 1

    # History should be updated (via try/finally)
    assert len(adapter.message_history) > 0


@pytest.mark.asyncio
async def test_streaming_cancel_without_tool_stops_immediately(monkeypatch):
    """Cancelling with no tool call in flight should stop right away."""
    events = [
        *_make_text_events(["Hello ", "world. ", "This is long."]),
    ]
    result = MockSlowRunResultStreaming(events)
    runner = MockRunner(stream_results=[result])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    token = CancelToken()
    collected = []
    async for event in adapter.run_streaming("test", cancel_token=token):
        collected.append(event)
        if event.type == AgentStreamEventType.TEXT_DELTA:
            token.cancel()

    text_deltas = [e for e in collected if e.type == AgentStreamEventType.TEXT_DELTA]
    assert len(text_deltas) == 1
    assert text_deltas[0].text == "Hello "


@pytest.mark.asyncio
async def test_notify_interruption_truncates_by_default(monkeypatch):
    """Default (truncate) mode replaces the last assistant message."""
    input_list = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello there, how can I help?"},
    ]
    runner = MockRunner(run_results=[MockRunResult(final_output="reply", input_list=input_list)])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    await adapter.run("hi")
    assert len(adapter.message_history) == 2

    adapter.notify_interruption("Hello there")
    assert len(adapter.message_history) == 2
    assert adapter.message_history[1]["content"] == "Hello there..."


@pytest.mark.asyncio
async def test_notify_interruption_truncate_falls_back_without_content(monkeypatch):
    """Truncate mode appends a note when assistant content is unavailable."""
    class AssistantToolOnlyMessage:
        role = "assistant"

    input_list = [
        {"role": "user", "content": "hi"},
        AssistantToolOnlyMessage(),
    ]
    runner = MockRunner(run_results=[MockRunResult(final_output="reply", input_list=input_list)])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    await adapter.run("hi")
    assert len(adapter.message_history) == 2

    adapter.notify_interruption("", mode="truncate")
    assert len(adapter.message_history) == 3
    assert adapter.message_history[2]["role"] == "developer"
    assert "interrupted" in adapter.message_history[2]["content"].lower()


@pytest.mark.asyncio
async def test_notify_interruption_truncate_does_not_corrupt_older_turn(monkeypatch):
    """Non-writable newest assistant must not cause older entries to be overwritten."""

    class AssistantToolOnlyMessage:
        role = "assistant"

    input_list = [
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "First reply"},
        {"role": "user", "content": "turn 2"},
        AssistantToolOnlyMessage(),  # newest assistant — no writable content
    ]
    runner = MockRunner(run_results=[MockRunResult(final_output="reply", input_list=input_list)])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    await adapter.run("turn 2")

    adapter.notify_interruption("", mode="truncate")

    # The older assistant dict must be untouched
    assert adapter.message_history[1]["content"] == "First reply"
    # A fallback developer note should have been appended instead
    assert adapter.message_history[-1]["role"] == "developer"
    assert "interrupted" in adapter.message_history[-1]["content"].lower()


@pytest.mark.asyncio
async def test_notify_interruption_message_mode(monkeypatch):
    """Message mode appends a developer message to the history."""
    input_list = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "reply"},
    ]
    runner = MockRunner(run_results=[MockRunResult(final_output="reply", input_list=input_list)])
    monkeypatch.setattr("easycat.agents.openai_agents.Runner", runner, raising=False)

    adapter = OpenAIAgentsAdapter(MockAgent())
    await adapter.run("hi")
    assert len(adapter.message_history) == 2

    adapter.notify_interruption("rep", mode="message")
    assert len(adapter.message_history) == 3
    assert adapter.message_history[2]["role"] == "developer"
    assert "interrupted" in adapter.message_history[2]["content"].lower()
