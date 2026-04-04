"""Tests for PydanticAIAdapter.

Uses lightweight mock objects that replicate PydanticAI's run/run_stream/iter API
surface so the tests run without pydantic-ai installed.
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from easycat.agent_runner import AgentStreamEventType
from easycat.agents.pydantic_ai import PydanticAIAdapter, _map_pydantic_event
from easycat.cancel import CancelToken

# ── Mock PydanticAI objects (run / run_stream fallback) ─────────────


@dataclass
class MockRunResult:
    """Mimics pydantic_ai.AgentRunResult."""

    output: Any
    _messages: list[Any] = field(default_factory=list)

    def new_messages(self) -> list[Any]:
        return list(self._messages)


class MockStreamResult:
    """Mimics pydantic_ai.StreamedRunResult (async context manager body)."""

    def __init__(self, chunks: list[str], messages: list[Any] | None = None) -> None:
        self._chunks = chunks
        self._messages = messages or []

    async def stream_text(self) -> AsyncIterator[str]:
        """Yield progressively accumulated text, like PydanticAI's stream_text()."""
        accumulated = ""
        for chunk in self._chunks:
            accumulated += chunk
            yield accumulated

    def new_messages(self) -> list[Any]:
        return list(self._messages)


class MockPydanticAgent:
    """Mimics a pydantic_ai.Agent with run() and run_stream()."""

    def __init__(
        self,
        responses: list[str] | None = None,
        stream_chunks: list[list[str]] | None = None,
    ) -> None:
        self._responses = list(responses or ["default response"])
        self._stream_chunks = list(stream_chunks or [["Hello", " world"]])
        self._call_count = 0
        self.run_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def run(
        self,
        prompt: str,
        *,
        message_history: list[Any] | None = None,
        deps: Any = None,
        model_settings: Any = None,
    ) -> MockRunResult:
        self.run_calls.append(
            {
                "prompt": prompt,
                "message_history": message_history,
                "deps": deps,
                "model_settings": model_settings,
            }
        )
        idx = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        response = self._responses[idx]
        return MockRunResult(
            output=response,
            _messages=[
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": response},
            ],
        )

    @asynccontextmanager
    async def run_stream(
        self,
        prompt: str,
        *,
        message_history: list[Any] | None = None,
        deps: Any = None,
        model_settings: Any = None,
    ) -> AsyncIterator[MockStreamResult]:
        self.stream_calls.append(
            {
                "prompt": prompt,
                "message_history": message_history,
                "deps": deps,
                "model_settings": model_settings,
            }
        )
        idx = min(self._call_count, len(self._stream_chunks) - 1)
        self._call_count += 1
        chunks = self._stream_chunks[idx]
        full_text = "".join(chunks)
        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": full_text},
        ]
        yield MockStreamResult(chunks=chunks, messages=messages)


class SlowMockPydanticAgent:
    """Mock agent whose stream_text yields slowly (for cancellation tests)."""

    def __init__(self) -> None:
        self._call_count = 0

    @asynccontextmanager
    async def run_stream(
        self,
        prompt: str,
        *,
        message_history: list[Any] | None = None,
        deps: Any = None,
        model_settings: Any = None,
    ) -> AsyncIterator[MockStreamResult]:
        class SlowStream(MockStreamResult):
            def __init__(self) -> None:
                super().__init__(chunks=[], messages=[])

            async def stream_text(self) -> AsyncIterator[str]:
                yield "Hello"
                await asyncio.sleep(0.1)
                yield "Hello world"
                await asyncio.sleep(0.1)
                yield "Hello world, how are you?"

        yield SlowStream()


class FailingStreamAgent:
    """Mock agent whose stream_text raises an exception."""

    @asynccontextmanager
    async def run_stream(
        self,
        prompt: str,
        *,
        message_history: list[Any] | None = None,
        deps: Any = None,
        model_settings: Any = None,
    ) -> AsyncIterator[MockStreamResult]:
        class FailingStream(MockStreamResult):
            def __init__(self) -> None:
                super().__init__(chunks=[], messages=[])

            async def stream_text(self) -> AsyncIterator[str]:
                yield "start"
                raise RuntimeError("stream exploded")

        yield FailingStream()


# ── Mock PydanticAI objects (iter() API — tool streaming) ──────────

# Class names MUST match what _map_pydantic_event checks via type().__name__


class TextPartDelta:
    """Mimics pydantic_ai.messages.TextPartDelta."""

    def __init__(self, content_delta: str = "") -> None:
        self.content_delta = content_delta


class ToolCallPartDelta:
    """Mimics pydantic_ai.messages.ToolCallPartDelta."""

    def __init__(self, args_delta: str = "") -> None:
        self.args_delta = args_delta


class MockDeltaEvent:
    """Generic event with a .delta attribute (like PartDeltaEvent)."""

    def __init__(self, delta: Any) -> None:
        self.delta = delta


class MockToolPart:
    """Mimics the part on a FunctionToolCallEvent."""

    def __init__(self, tool_name: str = "", tool_call_id: str = "") -> None:
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id


class FunctionToolCallEvent:
    """Mimics pydantic_ai FunctionToolCallEvent (class name must match exactly)."""

    def __init__(self, part: MockToolPart | None = None) -> None:
        self.part = part


class FunctionToolResultEvent:
    """Mimics pydantic_ai FunctionToolResultEvent (class name must match exactly)."""

    def __init__(self, tool_call_id: str = "", result: Any = "") -> None:
        self.tool_call_id = tool_call_id
        self.result = result


class MockNodeStream:
    """Async iterator of events yielded by node.stream()."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self._index = 0

    def __aiter__(self) -> MockNodeStream:
        return self

    async def __anext__(self) -> Any:
        if self._index >= len(self._events):
            raise StopAsyncIteration
        event = self._events[self._index]
        self._index += 1
        return event


class MockStreamableNode:
    """A node that has a stream() method (like ModelRequestNode or CallToolsNode)."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    @asynccontextmanager
    async def stream(self, ctx: Any) -> AsyncIterator[MockNodeStream]:
        yield MockNodeStream(self._events)


class MockEndNode:
    """A node without a stream() method (like End)."""

    pass


class MockIterAgentRun:
    """Mimics the object returned by agent.iter() — async iterable of nodes."""

    def __init__(self, nodes: list[Any], messages: list[Any] | None = None) -> None:
        self._nodes = nodes
        self._messages = messages or []
        self.ctx = object()

    def new_messages(self) -> list[Any]:
        return list(self._messages)

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._aiter_impl()

    async def _aiter_impl(self) -> AsyncIterator[Any]:
        for node in self._nodes:
            yield node


class MockIterPydanticAgent(MockPydanticAgent):
    """PydanticAI agent mock with iter() support for tool streaming tests."""

    def __init__(
        self,
        iter_nodes: list[Any] | None = None,
        iter_messages: list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._iter_nodes = iter_nodes or []
        self._iter_messages = iter_messages or []
        self.iter_calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def iter(
        self,
        prompt: str,
        *,
        message_history: list[Any] | None = None,
        deps: Any = None,
        model_settings: Any = None,
    ) -> AsyncIterator[MockIterAgentRun]:
        self.iter_calls.append(
            {
                "prompt": prompt,
                "message_history": message_history,
                "deps": deps,
                "model_settings": model_settings,
            }
        )
        yield MockIterAgentRun(self._iter_nodes, self._iter_messages)


class SlowMockIterAgent:
    """iter()-based agent with delays between events (for cancellation tests)."""

    def __init__(self) -> None:
        self.iter_calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def iter(
        self,
        prompt: str,
        *,
        message_history: list[Any] | None = None,
        deps: Any = None,
        model_settings: Any = None,
    ) -> AsyncIterator[MockIterAgentRun]:
        self.iter_calls.append({"prompt": prompt})

        class SlowNodeStream:
            """Stream that yields events with delays."""

            def __init__(self, events: list[Any]) -> None:
                self._events = events
                self._index = 0

            def __aiter__(self) -> SlowNodeStream:
                return self

            async def __anext__(self) -> Any:
                if self._index >= len(self._events):
                    raise StopAsyncIteration
                event = self._events[self._index]
                self._index += 1
                await asyncio.sleep(0.05)
                return event

        class SlowNode:
            def __init__(self, events: list[Any]) -> None:
                self._events = events

            @asynccontextmanager
            async def stream(self, ctx: Any) -> AsyncIterator[Any]:
                yield SlowNodeStream(self._events)

        events = [
            MockDeltaEvent(TextPartDelta("Hello")),
            MockDeltaEvent(TextPartDelta(" world")),
            MockDeltaEvent(TextPartDelta(" how are you?")),
        ]
        yield MockIterAgentRun([SlowNode(events)])


# ── Basic run() tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_returns_response():
    agent = MockPydanticAgent(responses=["Hello there!"])
    adapter = PydanticAIAdapter(agent)
    result = await adapter.run("Hi")
    assert result == "Hello there!"


@pytest.mark.asyncio
async def test_run_passes_prompt():
    agent = MockPydanticAgent(responses=["ok"])
    adapter = PydanticAIAdapter(agent)
    await adapter.run("What is 2+2?")
    assert agent.run_calls[0]["prompt"] == "What is 2+2?"


@pytest.mark.asyncio
async def test_run_passes_deps():
    agent = MockPydanticAgent(responses=["ok"])
    adapter = PydanticAIAdapter(agent, deps={"db": "postgres"})
    await adapter.run("query")
    assert agent.run_calls[0]["deps"] == {"db": "postgres"}


@pytest.mark.asyncio
async def test_run_passes_model_settings():
    agent = MockPydanticAgent(responses=["ok"])
    settings = {"temperature": 0.5}
    adapter = PydanticAIAdapter(agent, model_settings=settings)
    await adapter.run("query")
    assert agent.run_calls[0]["model_settings"] == settings


@pytest.mark.asyncio
async def test_run_converts_output_to_str():
    """Non-string outputs should be stringified."""

    class IntAgent:
        async def run(self, prompt, *, message_history=None, deps=None, model_settings=None):
            return MockRunResult(output=42, _messages=[])

    adapter = PydanticAIAdapter(IntAgent())
    result = await adapter.run("number")
    assert result == "42"
    assert isinstance(result, str)


# ── Multi-turn history tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_run_tracks_message_history():
    agent = MockPydanticAgent(responses=["first reply", "second reply"])
    adapter = PydanticAIAdapter(agent)

    await adapter.run("turn 1")
    assert len(adapter.message_history) == 2

    await adapter.run("turn 2")
    # Second call should have received the history from the first turn
    assert agent.run_calls[1]["message_history"] is not None
    assert len(agent.run_calls[1]["message_history"]) == 2


@pytest.mark.asyncio
async def test_run_first_call_no_history():
    agent = MockPydanticAgent(responses=["reply"])
    adapter = PydanticAIAdapter(agent)
    await adapter.run("hello")
    # First call should pass None for message_history
    assert agent.run_calls[0]["message_history"] is None


@pytest.mark.asyncio
async def test_clear_history():
    agent = MockPydanticAgent(responses=["reply1", "reply2"])
    adapter = PydanticAIAdapter(agent)

    await adapter.run("turn 1")
    assert len(adapter.message_history) > 0

    adapter.clear_history()
    assert adapter.message_history == []

    await adapter.run("fresh start")
    assert agent.run_calls[1]["message_history"] is None


# ── Streaming run_streaming() tests (run_stream fallback) ─────────


@pytest.mark.asyncio
async def test_streaming_fallback_yields_text_deltas():
    """When iter() is not available, falls back to run_stream()."""
    agent = MockPydanticAgent(stream_chunks=[["Hello", " world", "!"]])
    adapter = PydanticAIAdapter(agent)

    events = []
    async for event in adapter.run_streaming("greet"):
        events.append(event)

    text_deltas = [e for e in events if e.type == AgentStreamEventType.TEXT_DELTA]
    assert len(text_deltas) == 3
    assert text_deltas[0].text == "Hello"
    assert text_deltas[1].text == " world"
    assert text_deltas[2].text == "!"


@pytest.mark.asyncio
async def test_streaming_fallback_yields_done_event():
    agent = MockPydanticAgent(stream_chunks=[["Hi", " there"]])
    adapter = PydanticAIAdapter(agent)

    events = []
    async for event in adapter.run_streaming("greet"):
        events.append(event)

    done_events = [e for e in events if e.type == AgentStreamEventType.DONE]
    assert len(done_events) == 1
    assert done_events[0].text == "Hi there"


@pytest.mark.asyncio
async def test_streaming_fallback_updates_message_history():
    agent = MockPydanticAgent(stream_chunks=[["First", " response"], ["Second", " response"]])
    adapter = PydanticAIAdapter(agent)

    async for _ in adapter.run_streaming("turn 1"):
        pass
    assert len(adapter.message_history) == 2

    # Second call should receive history from first turn
    async for _ in adapter.run_streaming("turn 2"):
        pass
    assert agent.stream_calls[1]["message_history"] is not None
    assert len(agent.stream_calls[1]["message_history"]) == 2


@pytest.mark.asyncio
async def test_streaming_fallback_first_call_no_history():
    agent = MockPydanticAgent(stream_chunks=[["reply"]])
    adapter = PydanticAIAdapter(agent)

    async for _ in adapter.run_streaming("hello"):
        pass
    assert agent.stream_calls[0]["message_history"] is None


@pytest.mark.asyncio
async def test_streaming_fallback_passes_deps():
    agent = MockPydanticAgent(stream_chunks=[["ok"]])
    adapter = PydanticAIAdapter(agent, deps="my_deps")

    async for _ in adapter.run_streaming("query"):
        pass
    assert agent.stream_calls[0]["deps"] == "my_deps"


@pytest.mark.asyncio
async def test_streaming_fallback_passes_model_settings():
    agent = MockPydanticAgent(stream_chunks=[["ok"]])
    settings = {"max_tokens": 100}
    adapter = PydanticAIAdapter(agent, model_settings=settings)

    async for _ in adapter.run_streaming("query"):
        pass
    assert agent.stream_calls[0]["model_settings"] == settings


# ── Cancellation tests (run_stream fallback) ──────────────────────


@pytest.mark.asyncio
async def test_streaming_fallback_respects_cancel_token():
    adapter = PydanticAIAdapter(SlowMockPydanticAgent())
    token = CancelToken()

    events = []
    async for event in adapter.run_streaming("test", cancel_token=token):
        events.append(event)
        if event.type == AgentStreamEventType.TEXT_DELTA:
            token.cancel()

    text_deltas = [e for e in events if e.type == AgentStreamEventType.TEXT_DELTA]
    # Should stop after the first delta
    assert len(text_deltas) == 1
    assert text_deltas[0].text == "Hello"


@pytest.mark.asyncio
async def test_streaming_fallback_cancel_still_emits_done():
    adapter = PydanticAIAdapter(SlowMockPydanticAgent())
    token = CancelToken()

    events = []
    async for event in adapter.run_streaming("test", cancel_token=token):
        events.append(event)
        if event.type == AgentStreamEventType.TEXT_DELTA:
            token.cancel()

    done_events = [e for e in events if e.type == AgentStreamEventType.DONE]
    assert len(done_events) == 1


# ── Error handling tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_propagates_exception():
    adapter = PydanticAIAdapter(FailingStreamAgent())
    with pytest.raises(RuntimeError, match="stream exploded"):
        async for _ in adapter.run_streaming("test"):
            pass


# ── Context parameter is accepted ────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_accepts_context_param():
    """The context parameter should be accepted but not used."""
    agent = MockPydanticAgent(stream_chunks=[["ok"]])
    adapter = PydanticAIAdapter(agent)

    events = []
    async for event in adapter.run_streaming(
        "test",
        context=[{"role": "user", "content": "prior"}],
    ):
        events.append(event)

    # Should work and produce events regardless of context
    assert any(e.type == AgentStreamEventType.DONE for e in events)


# ── Protocol compatibility tests ─────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_has_run_streaming():
    """Session checks hasattr(agent, 'run_streaming')."""
    adapter = PydanticAIAdapter(MockPydanticAgent())
    assert hasattr(adapter, "run_streaming")
    assert callable(adapter.run_streaming)


@pytest.mark.asyncio
async def test_adapter_has_clear_history():
    """Session calls clear_history() if available."""
    adapter = PydanticAIAdapter(MockPydanticAgent())
    assert hasattr(adapter, "clear_history")
    assert callable(adapter.clear_history)


@pytest.mark.asyncio
async def test_adapter_has_run():
    """Basic Agent protocol requires run()."""
    adapter = PydanticAIAdapter(MockPydanticAgent())
    assert hasattr(adapter, "run")
    assert callable(adapter.run)


# ── Mixed run/streaming turns ────────────────────────────────────


@pytest.mark.asyncio
async def test_mixed_run_and_streaming_shares_history():
    """History should be shared across run() and run_streaming() calls."""
    agent = MockPydanticAgent(
        responses=["basic reply"],
        stream_chunks=[["streamed", " reply"]],
    )
    adapter = PydanticAIAdapter(agent)

    # First turn: basic run
    await adapter.run("turn 1")
    assert len(adapter.message_history) == 2

    # Second turn: streaming
    async for _ in adapter.run_streaming("turn 2"):
        pass
    # Stream call should have received history from the basic run
    assert agent.stream_calls[0]["message_history"] is not None
    assert len(agent.stream_calls[0]["message_history"]) == 2


# ── _map_pydantic_event unit tests ───────────────────────────────


class TestMapPydanticEvent:
    """Unit tests for the _map_pydantic_event helper."""

    def test_maps_text_part_delta(self):
        event = MockDeltaEvent(TextPartDelta("Hello"))
        result = _map_pydantic_event(event)
        assert result is not None
        assert result.type == AgentStreamEventType.TEXT_DELTA
        assert result.text == "Hello"

    def test_maps_tool_call_part_delta(self):
        event = MockDeltaEvent(ToolCallPartDelta('{"city": "SF"}'))
        result = _map_pydantic_event(event)
        assert result is not None
        assert result.type == AgentStreamEventType.TOOL_DELTA
        assert result.text == '{"city": "SF"}'

    def test_maps_function_tool_call_event(self):
        part = MockToolPart(tool_name="get_weather", tool_call_id="call_123")
        event = FunctionToolCallEvent(part=part)
        result = _map_pydantic_event(event)
        assert result is not None
        assert result.type == AgentStreamEventType.TOOL_STARTED
        assert result.tool_name == "get_weather"
        assert result.call_id == "call_123"

    def test_maps_function_tool_result_event(self):
        event = FunctionToolResultEvent(tool_call_id="call_123", result="sunny, 72F")
        result = _map_pydantic_event(event)
        assert result is not None
        assert result.type == AgentStreamEventType.TOOL_RESULT
        assert result.call_id == "call_123"
        assert result.result == "sunny, 72F"

    def test_skips_empty_text_delta(self):
        event = MockDeltaEvent(TextPartDelta(""))
        result = _map_pydantic_event(event)
        assert result is None

    def test_skips_empty_tool_delta(self):
        event = MockDeltaEvent(ToolCallPartDelta(""))
        result = _map_pydantic_event(event)
        assert result is None

    def test_skips_unknown_delta_type(self):
        class ThinkingPartDelta:
            pass

        event = MockDeltaEvent(ThinkingPartDelta())
        result = _map_pydantic_event(event)
        assert result is None

    def test_skips_unknown_event(self):
        class SomeOtherEvent:
            pass

        result = _map_pydantic_event(SomeOtherEvent())
        assert result is None

    def test_tool_result_with_none_result(self):
        """FunctionToolResultEvent without a result attribute."""
        event = FunctionToolResultEvent(tool_call_id="call_x")
        # Remove the result attribute to test the hasattr check
        delattr(event, "result")
        result = _map_pydantic_event(event)
        assert result is not None
        assert result.type == AgentStreamEventType.TOOL_RESULT
        assert result.result == ""

    def test_tool_started_with_none_part(self):
        """FunctionToolCallEvent with part=None."""
        event = FunctionToolCallEvent(part=None)
        result = _map_pydantic_event(event)
        assert result is not None
        assert result.type == AgentStreamEventType.TOOL_STARTED
        assert result.tool_name == ""
        assert result.call_id == ""


# ── iter() API streaming tests (text + tools) ────────────────────


@pytest.mark.asyncio
async def test_iter_streaming_text_only():
    """iter() path with text-only events yields TEXT_DELTA + DONE."""
    text_events = [
        MockDeltaEvent(TextPartDelta("Hello")),
        MockDeltaEvent(TextPartDelta(" world")),
    ]
    agent = MockIterPydanticAgent(
        iter_nodes=[MockStreamableNode(text_events)],
        iter_messages=[{"role": "user", "content": "hi"}],
    )
    adapter = PydanticAIAdapter(agent)

    events = []
    async for event in adapter.run_streaming("hi"):
        events.append(event)

    text_deltas = [e for e in events if e.type == AgentStreamEventType.TEXT_DELTA]
    assert len(text_deltas) == 2
    assert text_deltas[0].text == "Hello"
    assert text_deltas[1].text == " world"

    done = [e for e in events if e.type == AgentStreamEventType.DONE]
    assert len(done) == 1
    assert done[0].text == "Hello world"


@pytest.mark.asyncio
async def test_iter_streaming_tool_events():
    """iter() path yields TOOL_STARTED, TOOL_DELTA, TOOL_RESULT for tool calls."""
    tool_part = MockToolPart(tool_name="get_weather", tool_call_id="call_abc")

    tool_node_events = [
        FunctionToolCallEvent(part=tool_part),
        MockDeltaEvent(ToolCallPartDelta('{"city":')),
        MockDeltaEvent(ToolCallPartDelta(' "London"}')),
        FunctionToolResultEvent(tool_call_id="call_abc", result="rainy, 55F"),
    ]
    text_node_events = [
        MockDeltaEvent(TextPartDelta("The weather is ")),
        MockDeltaEvent(TextPartDelta("rainy.")),
    ]

    agent = MockIterPydanticAgent(
        iter_nodes=[
            MockStreamableNode(tool_node_events),
            MockStreamableNode(text_node_events),
        ],
        iter_messages=[{"role": "user", "content": "weather?"}],
    )
    adapter = PydanticAIAdapter(agent)

    events = []
    async for event in adapter.run_streaming("weather?"):
        events.append(event)

    types = [e.type for e in events]
    assert AgentStreamEventType.TOOL_STARTED in types
    assert AgentStreamEventType.TOOL_DELTA in types
    assert AgentStreamEventType.TOOL_RESULT in types
    assert AgentStreamEventType.TEXT_DELTA in types
    assert AgentStreamEventType.DONE in types

    # Verify tool started
    tool_started = [e for e in events if e.type == AgentStreamEventType.TOOL_STARTED]
    assert len(tool_started) == 1
    assert tool_started[0].tool_name == "get_weather"
    assert tool_started[0].call_id == "call_abc"

    # Verify tool deltas
    tool_deltas = [e for e in events if e.type == AgentStreamEventType.TOOL_DELTA]
    assert len(tool_deltas) == 2
    assert tool_deltas[0].text == '{"city":'
    assert tool_deltas[1].text == ' "London"}'

    # Verify tool result
    tool_result = [e for e in events if e.type == AgentStreamEventType.TOOL_RESULT]
    assert len(tool_result) == 1
    assert tool_result[0].call_id == "call_abc"
    assert tool_result[0].result == "rainy, 55F"

    # Verify text and done
    text_deltas = [e for e in events if e.type == AgentStreamEventType.TEXT_DELTA]
    assert text_deltas[0].text == "The weather is "
    assert text_deltas[1].text == "rainy."

    done = [e for e in events if e.type == AgentStreamEventType.DONE]
    assert done[0].text == "The weather is rainy."


@pytest.mark.asyncio
async def test_iter_streaming_skips_end_nodes():
    """Nodes without stream() (like End) are silently skipped."""
    text_events = [MockDeltaEvent(TextPartDelta("Hi"))]
    agent = MockIterPydanticAgent(
        iter_nodes=[
            MockStreamableNode(text_events),
            MockEndNode(),  # Should be skipped
        ],
    )
    adapter = PydanticAIAdapter(agent)

    events = []
    async for event in adapter.run_streaming("hi"):
        events.append(event)

    text_deltas = [e for e in events if e.type == AgentStreamEventType.TEXT_DELTA]
    assert len(text_deltas) == 1
    assert text_deltas[0].text == "Hi"


@pytest.mark.asyncio
async def test_iter_streaming_updates_history():
    """iter() path updates message history from agent_run.new_messages()."""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello!"},
    ]
    agent = MockIterPydanticAgent(
        iter_nodes=[MockStreamableNode([MockDeltaEvent(TextPartDelta("Hello!"))])],
        iter_messages=messages,
    )
    adapter = PydanticAIAdapter(agent)

    async for _ in adapter.run_streaming("hi"):
        pass

    assert len(adapter.message_history) == 2
    assert adapter.message_history[0] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_iter_streaming_multi_turn_history():
    """iter() path passes history on subsequent calls."""
    messages = [
        {"role": "user", "content": "t1"},
        {"role": "assistant", "content": "r1"},
    ]
    agent = MockIterPydanticAgent(
        iter_nodes=[MockStreamableNode([MockDeltaEvent(TextPartDelta("r1"))])],
        iter_messages=messages,
    )
    adapter = PydanticAIAdapter(agent)

    # First turn
    async for _ in adapter.run_streaming("t1"):
        pass
    assert agent.iter_calls[0]["message_history"] is None

    # Second turn — should pass history from first turn
    async for _ in adapter.run_streaming("t2"):
        pass
    assert agent.iter_calls[1]["message_history"] is not None
    assert len(agent.iter_calls[1]["message_history"]) == 2


@pytest.mark.asyncio
async def test_iter_streaming_passes_deps_and_settings():
    """iter() path forwards deps and model_settings."""
    agent = MockIterPydanticAgent(
        iter_nodes=[MockStreamableNode([MockDeltaEvent(TextPartDelta("ok"))])],
    )
    adapter = PydanticAIAdapter(agent, deps="my_deps", model_settings={"temp": 0.5})

    async for _ in adapter.run_streaming("query"):
        pass

    assert agent.iter_calls[0]["deps"] == "my_deps"
    assert agent.iter_calls[0]["model_settings"] == {"temp": 0.5}


@pytest.mark.asyncio
async def test_iter_streaming_prefers_iter_over_run_stream():
    """When agent has iter(), it should be used instead of run_stream()."""
    agent = MockIterPydanticAgent(
        iter_nodes=[MockStreamableNode([MockDeltaEvent(TextPartDelta("via iter"))])],
        stream_chunks=[["via run_stream"]],
    )
    adapter = PydanticAIAdapter(agent)

    events = []
    async for event in adapter.run_streaming("test"):
        events.append(event)

    # Should use iter(), not run_stream()
    assert len(agent.iter_calls) == 1
    assert len(agent.stream_calls) == 0

    text_deltas = [e for e in events if e.type == AgentStreamEventType.TEXT_DELTA]
    assert text_deltas[0].text == "via iter"


@pytest.mark.asyncio
async def test_iter_streaming_multiple_tool_calls():
    """iter() handles multiple tool calls across separate nodes."""
    tool_node_1 = MockStreamableNode(
        [
            FunctionToolCallEvent(MockToolPart("search", "call_1")),
            MockDeltaEvent(ToolCallPartDelta('{"q": "test"}')),
            FunctionToolResultEvent(tool_call_id="call_1", result="found 3 results"),
        ]
    )
    tool_node_2 = MockStreamableNode(
        [
            FunctionToolCallEvent(MockToolPart("fetch", "call_2")),
            MockDeltaEvent(ToolCallPartDelta('{"url": "http://example.com"}')),
            FunctionToolResultEvent(tool_call_id="call_2", result="page content"),
        ]
    )
    text_node = MockStreamableNode(
        [
            MockDeltaEvent(TextPartDelta("Here's what I found.")),
        ]
    )

    agent = MockIterPydanticAgent(
        iter_nodes=[tool_node_1, tool_node_2, text_node],
    )
    adapter = PydanticAIAdapter(agent)

    events = []
    async for event in adapter.run_streaming("research"):
        events.append(event)

    tool_started = [e for e in events if e.type == AgentStreamEventType.TOOL_STARTED]
    assert len(tool_started) == 2
    assert tool_started[0].tool_name == "search"
    assert tool_started[1].tool_name == "fetch"

    tool_results = [e for e in events if e.type == AgentStreamEventType.TOOL_RESULT]
    assert len(tool_results) == 2


# ── iter() cancellation tests ────────────────────────────────────


@pytest.mark.asyncio
async def test_iter_streaming_respects_cancel_token():
    """iter() path stops on cancellation."""
    adapter = PydanticAIAdapter(SlowMockIterAgent())
    token = CancelToken()

    events = []
    async for event in adapter.run_streaming("test", cancel_token=token):
        events.append(event)
        if event.type == AgentStreamEventType.TEXT_DELTA:
            token.cancel()

    text_deltas = [e for e in events if e.type == AgentStreamEventType.TEXT_DELTA]
    assert len(text_deltas) == 1
    assert text_deltas[0].text == "Hello"


@pytest.mark.asyncio
async def test_iter_streaming_cancel_mid_text_node_stops_consuming_stream():
    """Cancellation in a text node should stop that node's stream promptly."""

    class CountingNodeStream:
        def __init__(self, events: list[Any]) -> None:
            self._events = events
            self._index = 0
            self.reads = 0

        def __aiter__(self) -> CountingNodeStream:
            return self

        async def __anext__(self) -> Any:
            if self._index >= len(self._events):
                raise StopAsyncIteration
            event = self._events[self._index]
            self._index += 1
            self.reads += 1
            return event

    class CountingModelRequestNode:
        def __init__(self, events: list[Any]) -> None:
            self._events = events
            self.stream_reads = 0

        @asynccontextmanager
        async def stream(self, ctx: Any) -> AsyncIterator[CountingNodeStream]:
            node_stream = CountingNodeStream(self._events)
            try:
                yield node_stream
            finally:
                self.stream_reads = node_stream.reads

    CountingModelRequestNode.__name__ = "ModelRequestNode"

    node = CountingModelRequestNode(
        [
            MockDeltaEvent(TextPartDelta("first")),
            MockDeltaEvent(TextPartDelta(" second")),
            MockDeltaEvent(TextPartDelta(" third")),
        ]
    )
    agent = MockIterPydanticAgent(iter_nodes=[node])
    adapter = PydanticAIAdapter(agent)
    token = CancelToken()

    events = []
    async for event in adapter.run_streaming("test", cancel_token=token):
        events.append(event)
        if event.type == AgentStreamEventType.TEXT_DELTA:
            token.cancel()

    text_deltas = [e for e in events if e.type == AgentStreamEventType.TEXT_DELTA]
    assert len(text_deltas) == 1
    assert text_deltas[0].text == "first"
    # First event is yielded; one additional event is observed after cancel,
    # then the node stream exits without draining the rest.
    assert node.stream_reads == 2


@pytest.mark.asyncio
async def test_iter_streaming_cancel_emits_done():
    """iter() path emits DONE even after cancellation."""
    adapter = PydanticAIAdapter(SlowMockIterAgent())
    token = CancelToken()

    events = []
    async for event in adapter.run_streaming("test", cancel_token=token):
        events.append(event)
        if event.type == AgentStreamEventType.TEXT_DELTA:
            token.cancel()

    done = [e for e in events if e.type == AgentStreamEventType.DONE]
    assert len(done) == 1


@pytest.mark.asyncio
async def test_iter_streaming_cancel_between_nodes():
    """Cancellation between nodes stops iteration."""
    events1 = [MockDeltaEvent(TextPartDelta("first"))]
    events2 = [MockDeltaEvent(TextPartDelta("second"))]

    agent = MockIterPydanticAgent(
        iter_nodes=[
            MockStreamableNode(events1),
            MockStreamableNode(events2),
        ],
    )
    adapter = PydanticAIAdapter(agent)
    token = CancelToken()

    collected = []
    async for event in adapter.run_streaming("test", cancel_token=token):
        collected.append(event)
        if event.type == AgentStreamEventType.TEXT_DELTA:
            token.cancel()

    text_deltas = [e for e in collected if e.type == AgentStreamEventType.TEXT_DELTA]
    # Should see "first" but NOT "second" since cancel happened after first delta
    assert len(text_deltas) == 1
    assert text_deltas[0].text == "first"


# ── iter() with empty/skipped events ─────────────────────────────


@pytest.mark.asyncio
async def test_iter_streaming_empty_nodes():
    """iter() with no nodes yields only DONE."""
    agent = MockIterPydanticAgent(iter_nodes=[])
    adapter = PydanticAIAdapter(agent)

    events = []
    async for event in adapter.run_streaming("test"):
        events.append(event)

    assert len(events) == 1
    assert events[0].type == AgentStreamEventType.DONE
    assert events[0].text == ""


@pytest.mark.asyncio
async def test_iter_streaming_skips_unmapped_events():
    """Events that _map_pydantic_event returns None for are silently skipped."""

    class UnknownEvent:
        pass

    agent = MockIterPydanticAgent(
        iter_nodes=[
            MockStreamableNode(
                [
                    UnknownEvent(),
                    MockDeltaEvent(TextPartDelta("visible")),
                    UnknownEvent(),
                ]
            ),
        ],
    )
    adapter = PydanticAIAdapter(agent)

    events = []
    async for event in adapter.run_streaming("test"):
        events.append(event)

    text_deltas = [e for e in events if e.type == AgentStreamEventType.TEXT_DELTA]
    assert len(text_deltas) == 1
    assert text_deltas[0].text == "visible"


# ── Structured output tests ──────────────────────────────────────


class FakePydanticModel:
    """Fake Pydantic v2 model for testing structured output."""

    def __init__(self, name: str, score: int) -> None:
        self.name = name
        self.score = score

    def model_dump_json(self) -> str:
        return f'{{"name":"{self.name}","score":{self.score}}}'


@pytest.mark.asyncio
async def test_run_serializes_pydantic_model():
    """run() should use serialize_output for Pydantic models."""
    model = FakePydanticModel("Alice", 95)

    class StructuredAgent:
        async def run(self, prompt, *, message_history=None, deps=None, model_settings=None):
            return MockRunResult(output=model, _messages=[])

    adapter = PydanticAIAdapter(StructuredAgent())
    result = await adapter.run("query")
    assert result == '{"name":"Alice","score":95}'
    assert adapter.last_output is model


@pytest.mark.asyncio
async def test_run_stores_last_output():
    """run() should store the raw output in last_output."""
    agent = MockPydanticAgent(responses=["hello"])
    adapter = PydanticAIAdapter(agent)
    await adapter.run("test")
    assert adapter.last_output == "hello"


@pytest.mark.asyncio
async def test_run_serializes_dict():
    """run() should serialize dict output as JSON."""

    class DictAgent:
        async def run(self, prompt, *, message_history=None, deps=None, model_settings=None):
            return MockRunResult(output={"action": "greet"}, _messages=[])

    adapter = PydanticAIAdapter(DictAgent())
    result = await adapter.run("query")
    assert '"action"' in result
    assert '"greet"' in result


@pytest.mark.asyncio
async def test_iter_streaming_done_includes_structured_output():
    """iter() DONE event should carry structured_output."""
    model = FakePydanticModel("Bob", 80)

    class MockIterRunWithOutput(MockIterAgentRun):
        def __init__(self, nodes, messages=None):
            super().__init__(nodes, messages)
            self.output = model  # Structured output

    class IterAgentWithOutput(MockIterPydanticAgent):
        @asynccontextmanager
        async def iter(self, prompt, *, message_history=None, deps=None, model_settings=None):
            self.iter_calls.append({"prompt": prompt})
            yield MockIterRunWithOutput(self._iter_nodes, self._iter_messages)

    agent = IterAgentWithOutput(
        iter_nodes=[MockStreamableNode([MockDeltaEvent(TextPartDelta("Hi"))])],
    )
    adapter = PydanticAIAdapter(agent)

    events = []
    async for event in adapter.run_streaming("test"):
        events.append(event)

    done = [e for e in events if e.type == AgentStreamEventType.DONE]
    assert len(done) == 1
    assert done[0].structured_output is model
    assert adapter.last_output is model


@pytest.mark.asyncio
async def test_fallback_streaming_done_includes_structured_output():
    """run_stream() fallback DONE event should carry structured_output when available."""

    class StreamResultWithOutput(MockStreamResult):
        def __init__(self, chunks, messages=None, output=None):
            super().__init__(chunks, messages)
            self.output = output

    class AgentWithStreamOutput:
        @asynccontextmanager
        async def run_stream(
            self,
            prompt,
            *,
            message_history=None,
            deps=None,
            model_settings=None,
        ):
            yield StreamResultWithOutput(
                chunks=["Hello"],
                messages=[],
                output={"structured": True},
            )

    adapter = PydanticAIAdapter(AgentWithStreamOutput())
    events = []
    async for event in adapter.run_streaming("test"):
        events.append(event)

    done = [e for e in events if e.type == AgentStreamEventType.DONE]
    assert done[0].structured_output == {"structured": True}
    assert adapter.last_output == {"structured": True}


@pytest.mark.asyncio
async def test_clear_history_resets_last_output():
    agent = MockPydanticAgent(responses=["reply"])
    adapter = PydanticAIAdapter(agent)
    await adapter.run("test")
    assert adapter.last_output is not None

    adapter.clear_history()
    assert adapter.last_output is None


# ── Barge-in / interruption tests ─────────────────────────────────


class CallToolsNode(MockStreamableNode):
    """Named to match PydanticAI's CallToolsNode so the adapter
    recognises it as a tool node during cancellation drain."""

    pass


class ModelRequestNode(MockStreamableNode):
    """Named to match PydanticAI's ModelRequestNode (text generation)."""

    pass


class SlowToolIterAgent:
    """iter()-based agent with a tool call node followed by a text node,
    with delays so cancellation can arrive mid-tool."""

    def __init__(self) -> None:
        self.iter_calls: list[dict[str, Any]] = []
        self._iter_messages: list[Any] = []

    @asynccontextmanager
    async def iter(
        self,
        prompt: str,
        *,
        message_history: list[Any] | None = None,
        deps: Any = None,
        model_settings: Any = None,
    ) -> AsyncIterator[MockIterAgentRun]:
        self.iter_calls.append({"prompt": prompt})

        class SlowNodeStream:
            def __init__(self, events: list[Any]) -> None:
                self._events = events
                self._index = 0

            def __aiter__(self) -> SlowNodeStream:
                return self

            async def __anext__(self) -> Any:
                if self._index >= len(self._events):
                    raise StopAsyncIteration
                event = self._events[self._index]
                self._index += 1
                await asyncio.sleep(0.03)
                return event

        class SlowCallToolsNode:
            """Named CallToolsNode so the adapter recognises it."""

            def __init__(self, events: list[Any]) -> None:
                self._events = events

            @asynccontextmanager
            async def stream(self, ctx: Any) -> AsyncIterator[Any]:
                yield SlowNodeStream(self._events)

        # Must use __qualname__ trick to ensure type().__name__ == "CallToolsNode"
        SlowCallToolsNode.__name__ = "CallToolsNode"

        tool_events = [
            FunctionToolCallEvent(MockToolPart("db_update", "call_1")),
            MockDeltaEvent(ToolCallPartDelta('{"id": 1}')),
            FunctionToolResultEvent(tool_call_id="call_1", result="updated"),
        ]
        text_events = [
            MockDeltaEvent(TextPartDelta("Done updating.")),
        ]

        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "Done updating."},
        ]
        self._iter_messages = messages

        yield MockIterAgentRun(
            nodes=[
                SlowCallToolsNode(tool_events),
                ModelRequestNode(text_events),
            ],
            messages=messages,
        )


@pytest.mark.asyncio
async def test_iter_cancel_during_tool_call_lets_tool_complete():
    """When cancelled mid-tool-call via iter(), the tool node should
    complete and yield its result, but the text node should be skipped."""
    agent = SlowToolIterAgent()
    adapter = PydanticAIAdapter(agent)
    token = CancelToken()

    events = []
    async for event in adapter.run_streaming("update it", cancel_token=token):
        events.append(event)
        # Cancel after seeing the tool started
        if event.type == AgentStreamEventType.TOOL_STARTED:
            token.cancel()

    types = [e.type for e in events]

    # Tool events should all be present
    assert AgentStreamEventType.TOOL_STARTED in types
    assert AgentStreamEventType.TOOL_RESULT in types

    # Text deltas should be absent (the ModelRequestNode was skipped)
    assert AgentStreamEventType.TEXT_DELTA not in types

    # DONE should still be emitted
    assert AgentStreamEventType.DONE in types

    # History should be updated
    assert len(adapter.message_history) > 0


@pytest.mark.asyncio
async def test_notify_interruption_truncates_by_default():
    """Default (truncate) mode replaces the last ModelResponse TextPart."""
    try:
        from pydantic_ai.messages import ModelResponse  # noqa: F401

        pydantic_ai_available = True
    except ImportError:
        pydantic_ai_available = False

    agent = MockPydanticAgent(responses=["Hello there, how can I help?"])
    adapter = PydanticAIAdapter(agent)
    await adapter.run("hi")
    initial_len = len(adapter.message_history)

    adapter.notify_interruption("Hello there")

    if pydantic_ai_available:
        # Length stays the same — no new message, just truncation
        assert len(adapter.message_history) == initial_len
        # Find the last ModelResponse and check its TextPart was truncated
        for msg in reversed(adapter.message_history):
            if isinstance(msg, ModelResponse):
                text_parts = [p for p in msg.parts if type(p).__name__ == "TextPart"]
                assert len(text_parts) > 0
                assert text_parts[0].content == "Hello there..."
                break
    else:
        # Without pydantic_ai installed, notify_interruption is a no-op
        assert len(adapter.message_history) == initial_len


def test_notify_interruption_truncate_handles_frozen_text_part(monkeypatch):
    """truncate mode should mutate frozen TextPart content via object.__setattr__."""

    class TextPart:
        def __init__(self, content: str) -> None:
            object.__setattr__(self, "content", content)

        def __setattr__(self, name: str, value: Any) -> None:
            raise TypeError("frozen")

    class ModelResponse:
        def __init__(self, parts: list[Any]) -> None:
            self.parts = parts

    class ModelRequest:
        def __init__(self, parts: list[Any]) -> None:
            self.parts = parts

    class SystemPromptPart:
        def __init__(self, content: str) -> None:
            self.content = content

    messages_module = types.ModuleType("pydantic_ai.messages")
    messages_module.ModelResponse = ModelResponse
    messages_module.ModelRequest = ModelRequest
    messages_module.SystemPromptPart = SystemPromptPart

    package_module = types.ModuleType("pydantic_ai")
    package_module.messages = messages_module
    monkeypatch.setitem(sys.modules, "pydantic_ai", package_module)
    monkeypatch.setitem(sys.modules, "pydantic_ai.messages", messages_module)

    adapter = PydanticAIAdapter(MockPydanticAgent())
    response = ModelResponse(parts=[TextPart("Original response")])
    adapter._message_history = [response]

    adapter.notify_interruption("Original")

    assert response.parts[0].content == "Original..."
    assert len(adapter.message_history) == 1


@pytest.mark.asyncio
async def test_notify_interruption_message_mode():
    """Message mode appends a SystemPromptPart to the PydanticAI history."""
    try:
        from pydantic_ai.messages import ModelRequest, SystemPromptPart  # noqa: F401

        pydantic_ai_available = True
    except ImportError:
        pydantic_ai_available = False

    agent = MockPydanticAgent(responses=["reply"])
    adapter = PydanticAIAdapter(agent)
    await adapter.run("hi")
    initial_len = len(adapter.message_history)

    adapter.notify_interruption("rep", mode="message")

    if pydantic_ai_available:
        assert len(adapter.message_history) == initial_len + 1
        last = adapter.message_history[-1]
        assert hasattr(last, "parts")
        assert any("interrupted" in getattr(p, "content", "").lower() for p in last.parts)
    else:
        assert len(adapter.message_history) == initial_len


# ── deps property ─────────────────────────────────────────────────


def test_deps_getter():
    """deps property returns the initial dependency value."""
    agent = MockIterPydanticAgent(responses=["ok"])
    adapter = PydanticAIAdapter(agent, deps={"user_id": "123"})
    assert adapter.deps == {"user_id": "123"}


def test_deps_setter():
    """deps setter updates the dependency value for subsequent calls."""
    agent = MockIterPydanticAgent(responses=["ok"])
    adapter = PydanticAIAdapter(agent, deps={"user_id": "123"})
    adapter.deps = {"user_id": "456", "session": "abc"}
    assert adapter.deps == {"user_id": "456", "session": "abc"}
    assert adapter._deps == {"user_id": "456", "session": "abc"}


def test_deps_default_none():
    """deps defaults to None when not provided."""
    agent = MockIterPydanticAgent(responses=["ok"])
    adapter = PydanticAIAdapter(agent)
    assert adapter.deps is None
