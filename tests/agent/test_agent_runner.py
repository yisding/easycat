"""Tests for AgentRunner: context management, timeout, streaming, cancellation, and tracing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat.agent_runner import (
    AgentRunner,
    AgentRunnerConfig,
    AgentStreamEvent,
    AgentStreamEventType,
    AgentTimeoutError,
    StreamingAgent,
)
from easycat.cancel import CancelToken
from easycat.tracing import TraceContext

# ── Test agents ────────────────────────────────────────────────────


class EchoAgent:
    """Simple agent that echoes input with a prefix."""

    async def run(self, text: str) -> str:
        return f"Echo: {text}"


class UpperAgent:
    """Agent that uppercases input."""

    async def run(self, text: str) -> str:
        return text.upper()


class FailingAgent:
    """Agent that always raises."""

    async def run(self, text: str) -> str:
        raise ValueError("agent broke")


class HangingAgent:
    """Agent that hangs forever."""

    async def run(self, text: str) -> str:
        await asyncio.sleep(999)
        return "never"


class StreamingEchoAgent:
    """Streaming agent that yields text word by word."""

    async def run(self, text: str) -> str:
        return f"Echo: {text}"

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        words = text.split()
        for i, word in enumerate(words):
            delta = word if i == 0 else f" {word}"
            yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=delta)
        yield AgentStreamEvent(type=AgentStreamEventType.DONE, text=" ".join(words))


class StreamingToolAgent:
    """Streaming agent that invokes a tool during response."""

    async def run(self, text: str) -> str:
        return f"Result for {text}"

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_STARTED,
            tool_name="lookup",
            call_id="call_1",
        )
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_DELTA,
            call_id="call_1",
            text="searching...",
        )
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_RESULT,
            call_id="call_1",
            result="found it",
        )
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="Here is the answer.")
        yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="Here is the answer.")


class SlowStreamingAgent:
    """Streaming agent with delays between events."""

    async def run(self, text: str) -> str:
        return text

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="Hello ")
        await asyncio.sleep(0.05)
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="world")
        yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="Hello world")


class FailingStreamingAgent:
    """Streaming agent that raises mid-stream."""

    async def run(self, text: str) -> str:
        return text

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="start ")
        raise RuntimeError("stream broke")


class ContextAwareAgent:
    """Agent that records the context it receives."""

    received_contexts: list[list[dict[str, str]] | None]

    def __init__(self) -> None:
        self.received_contexts = []

    async def run(self, text: str) -> str:
        return f"reply to {text}"

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        self.received_contexts.append(context)
        response = f"reply to {text}"
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=response)
        yield AgentStreamEvent(type=AgentStreamEventType.DONE, text=response)


# ── Basic run() tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_returns_response():
    runner = AgentRunner(EchoAgent())
    result = await runner.run("hello")
    assert result == "Echo: hello"


@pytest.mark.asyncio
async def test_run_records_history():
    runner = AgentRunner(EchoAgent())
    await runner.run("hello")
    assert runner.history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Echo: hello"},
    ]


@pytest.mark.asyncio
async def test_run_multi_turn_history():
    runner = AgentRunner(UpperAgent())
    await runner.run("first")
    await runner.run("second")
    assert len(runner.history) == 4
    assert runner.history[0] == {"role": "user", "content": "first"}
    assert runner.history[1] == {"role": "assistant", "content": "FIRST"}
    assert runner.history[2] == {"role": "user", "content": "second"}
    assert runner.history[3] == {"role": "assistant", "content": "SECOND"}


@pytest.mark.asyncio
async def test_run_clear_history():
    runner = AgentRunner(EchoAgent())
    await runner.run("hello")
    assert len(runner.history) == 2
    runner.clear_history()
    assert runner.history == []


@pytest.mark.asyncio
async def test_run_timeout():
    config = AgentRunnerConfig(timeout=0.05)
    runner = AgentRunner(HangingAgent(), config)
    with pytest.raises(AgentTimeoutError) as exc_info:
        await runner.run("test")
    assert exc_info.value.timeout == 0.05
    # History should be rolled back on timeout
    assert runner.history == []


@pytest.mark.asyncio
async def test_run_agent_exception():
    runner = AgentRunner(FailingAgent())
    with pytest.raises(ValueError, match="agent broke"):
        await runner.run("test")
    # History should be rolled back on exception
    assert runner.history == []


@pytest.mark.asyncio
async def test_run_no_timeout():
    config = AgentRunnerConfig(timeout=None)
    runner = AgentRunner(EchoAgent(), config)
    result = await runner.run("hello")
    assert result == "Echo: hello"


# ── Tracing span tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_tracing_spans():
    runner = AgentRunner(EchoAgent())
    await runner.run("hello")

    spans = runner.spans
    assert len(spans) == 1
    assert spans[0].name == "agent_execution"
    assert spans[0].end_time is not None
    assert spans[0].duration_ms is not None
    assert spans[0].duration_ms >= 0


@pytest.mark.asyncio
async def test_run_tracing_disabled():
    config = AgentRunnerConfig(enable_tracing=False)
    runner = AgentRunner(EchoAgent(), config)
    await runner.run("hello")
    assert runner.spans == []


@pytest.mark.asyncio
async def test_run_tracing_on_timeout():
    config = AgentRunnerConfig(timeout=0.05)
    runner = AgentRunner(HangingAgent(), config)
    with pytest.raises(AgentTimeoutError):
        await runner.run("test")
    spans = runner.spans
    assert len(spans) == 1
    assert spans[0].metadata.get("error_type") == "AgentTimeoutError"
    assert spans[0].end_time is not None


@pytest.mark.asyncio
async def test_run_tracing_on_exception():
    runner = AgentRunner(FailingAgent())
    with pytest.raises(ValueError):
        await runner.run("test")
    spans = runner.spans
    assert len(spans) == 1
    assert spans[0].metadata.get("error_type") == "ValueError"


@pytest.mark.asyncio
async def test_clear_spans():
    runner = AgentRunner(EchoAgent())
    await runner.run("hello")
    assert len(runner.spans) > 0
    runner.clear_spans()
    assert runner.spans == []


# ── Span unit tests ────────────────────────────────────────────────


def test_span_duration_none_before_finish():
    ctx = TraceContext()
    span = ctx.create_span(name="test")
    assert span.duration_ms is None


def test_span_finish():
    ctx = TraceContext()
    span = ctx.create_span(name="test")
    span.finish()
    assert span.end_time is not None
    assert span.duration_ms is not None
    assert span.duration_ms >= 0


# ── StreamingAgent protocol tests ──────────────────────────────────


def test_streaming_agent_protocol_detection():
    assert isinstance(StreamingEchoAgent(), StreamingAgent)
    assert not isinstance(EchoAgent(), StreamingAgent)


def test_agent_runner_is_streaming():
    assert AgentRunner(StreamingEchoAgent()).is_streaming
    assert not AgentRunner(EchoAgent()).is_streaming


# ── Streaming run_streaming() tests ────────────────────────────────


@pytest.mark.asyncio
async def test_run_streaming_with_streaming_agent():
    runner = AgentRunner(StreamingEchoAgent())
    events = []
    async for event in runner.run_streaming("hello world"):
        events.append(event)

    # Should have text deltas + done
    text_deltas = [e for e in events if e.type == AgentStreamEventType.TEXT_DELTA]
    done_events = [e for e in events if e.type == AgentStreamEventType.DONE]
    assert len(text_deltas) == 2  # "hello" and " world"
    assert text_deltas[0].text == "hello"
    assert text_deltas[1].text == " world"
    assert len(done_events) == 1
    assert done_events[0].text == "hello world"


@pytest.mark.asyncio
async def test_run_streaming_accumulates_history():
    runner = AgentRunner(StreamingEchoAgent())
    async for _ in runner.run_streaming("hello world"):
        pass

    assert len(runner.history) == 2
    assert runner.history[0] == {"role": "user", "content": "hello world"}
    assert runner.history[1] == {"role": "assistant", "content": "hello world"}


@pytest.mark.asyncio
async def test_run_streaming_non_streaming_fallback():
    """Non-streaming agent wrapped by run_streaming should yield delta+done."""
    runner = AgentRunner(EchoAgent())
    events = []
    async for event in runner.run_streaming("test"):
        events.append(event)

    assert len(events) == 2
    assert events[0].type == AgentStreamEventType.TEXT_DELTA
    assert events[0].text == "Echo: test"
    assert events[1].type == AgentStreamEventType.DONE
    assert events[1].text == "Echo: test"


@pytest.mark.asyncio
async def test_run_streaming_non_streaming_timeout():
    config = AgentRunnerConfig(timeout=0.05)
    runner = AgentRunner(HangingAgent(), config)
    with pytest.raises(AgentTimeoutError):
        async for _ in runner.run_streaming("test"):
            pass
    assert runner.history == []


@pytest.mark.asyncio
async def test_run_streaming_tool_events():
    runner = AgentRunner(StreamingToolAgent())
    events = []
    async for event in runner.run_streaming("lookup something"):
        events.append(event)

    types = [e.type for e in events]
    assert AgentStreamEventType.TOOL_STARTED in types
    assert AgentStreamEventType.TOOL_DELTA in types
    assert AgentStreamEventType.TOOL_RESULT in types
    assert AgentStreamEventType.TEXT_DELTA in types

    tool_started = [e for e in events if e.type == AgentStreamEventType.TOOL_STARTED][0]
    assert tool_started.tool_name == "lookup"
    assert tool_started.call_id == "call_1"

    tool_result = [e for e in events if e.type == AgentStreamEventType.TOOL_RESULT][0]
    assert tool_result.result == "found it"


@pytest.mark.asyncio
async def test_run_streaming_cancel_token():
    """Cancellation should stop consuming the stream."""
    token = CancelToken()
    runner = AgentRunner(SlowStreamingAgent())

    events = []
    async for event in runner.run_streaming("test", cancel_token=token):
        events.append(event)
        if event.type == AgentStreamEventType.TEXT_DELTA:
            token.cancel()  # Cancel after first delta

    # Should have received at most the first delta
    text_deltas = [e for e in events if e.type == AgentStreamEventType.TEXT_DELTA]
    assert len(text_deltas) == 1
    assert text_deltas[0].text == "Hello "


@pytest.mark.asyncio
async def test_run_streaming_exception():
    runner = AgentRunner(FailingStreamingAgent())
    with pytest.raises(RuntimeError, match="stream broke"):
        async for _ in runner.run_streaming("test"):
            pass
    # History rolled back on error
    assert runner.history == []


# ── Streaming tracing span tests ──────────────────────────────────


@pytest.mark.asyncio
async def test_run_streaming_tracing_spans():
    runner = AgentRunner(StreamingEchoAgent())
    async for _ in runner.run_streaming("hello"):
        pass

    span_names = [s.name for s in runner.spans]
    assert "stt_to_agent" in span_names
    assert "agent_execution" in span_names
    assert "agent_to_tts" in span_names

    # All spans should be finished
    for span in runner.spans:
        assert span.end_time is not None
        assert span.duration_ms is not None


@pytest.mark.asyncio
async def test_run_streaming_tracing_non_streaming_fallback():
    runner = AgentRunner(EchoAgent())
    async for _ in runner.run_streaming("test"):
        pass

    span_names = [s.name for s in runner.spans]
    assert "stt_to_agent" in span_names
    assert "agent_execution" in span_names
    assert "agent_to_tts" in span_names


@pytest.mark.asyncio
async def test_run_streaming_tracing_on_error():
    runner = AgentRunner(FailingStreamingAgent())
    with pytest.raises(RuntimeError):
        async for _ in runner.run_streaming("test"):
            pass

    exec_spans = [s for s in runner.spans if s.name == "agent_execution"]
    assert len(exec_spans) == 1
    assert exec_spans[0].metadata.get("error_type") == "RuntimeError"


# ── Context passing tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_context_passing():
    agent = ContextAwareAgent()
    runner = AgentRunner(agent)

    # First turn — no prior context
    async for _ in runner.run_streaming("hello"):
        pass
    assert agent.received_contexts[0] == []

    # Second turn — should see first turn's history
    async for _ in runner.run_streaming("follow up"):
        pass
    assert len(agent.received_contexts) == 2
    ctx = agent.received_contexts[1]
    assert len(ctx) == 2  # user + assistant from turn 1
    assert ctx[0] == {"role": "user", "content": "hello"}
    assert ctx[1] == {"role": "assistant", "content": "reply to hello"}


@pytest.mark.asyncio
async def test_streaming_context_cleared():
    agent = ContextAwareAgent()
    runner = AgentRunner(agent)

    async for _ in runner.run_streaming("hello"):
        pass
    runner.clear_history()

    async for _ in runner.run_streaming("fresh start"):
        pass
    assert agent.received_contexts[1] == []  # No context after clear


# ── Barge-in tool call completion tests ────────────────────────────


class SlowToolStreamingAgent:
    """Agent that starts a tool call, then yields text — with a delay
    between TOOL_STARTED and TOOL_RESULT so cancellation can arrive
    mid-tool.
    """

    async def run(self, text: str) -> str:
        return f"Result for {text}"

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        # Text before tool call
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="Let me check. ")
        # Start tool call
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_STARTED,
            tool_name="lookup",
            call_id="call_1",
        )
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_DELTA,
            call_id="call_1",
            text="searching...",
        )
        # Simulate slow tool execution — cancel arrives here
        await asyncio.sleep(0.1)
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_RESULT,
            call_id="call_1",
            result="found it",
        )
        # Text after tool
        yield AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text="Here is the answer.")
        yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="Here is the answer.")


class MultiToolDrainAgent:
    """Agent that starts additional tool calls while cancellation drain is active."""

    async def run(self, text: str) -> str:
        return text

    async def run_streaming(
        self,
        text: str,
        *,
        context: list[dict[str, str]] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_STARTED,
            tool_name="first",
            call_id="call_1",
        )
        await asyncio.sleep(0)
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_STARTED,
            tool_name="second",
            call_id="call_2",
        )
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_RESULT,
            call_id="call_1",
            result="first done",
        )
        yield AgentStreamEvent(
            type=AgentStreamEventType.TOOL_RESULT,
            call_id="call_2",
            result="second done",
        )
        yield AgentStreamEvent(type=AgentStreamEventType.DONE, text="done")


@pytest.mark.asyncio
async def test_cancel_during_tool_call_lets_tool_complete():
    """When cancelled mid-tool-call, the stream should continue until the
    TOOL_RESULT is received, then stop."""
    token = CancelToken()
    runner = AgentRunner(SlowToolStreamingAgent())

    events: list[AgentStreamEvent] = []
    async for event in runner.run_streaming("test", cancel_token=token):
        events.append(event)
        # Cancel after seeing the TOOL_STARTED event
        if event.type == AgentStreamEventType.TOOL_STARTED:
            token.cancel()

    types = [e.type for e in events]
    # Tool events should all be present — the tool completed
    assert AgentStreamEventType.TOOL_STARTED in types
    assert AgentStreamEventType.TOOL_RESULT in types
    # The initial text delta before cancellation should be there
    assert AgentStreamEventType.TEXT_DELTA in types
    # The DONE event should NOT be present (we stopped after tool completed)
    assert AgentStreamEventType.DONE not in types


@pytest.mark.asyncio
async def test_cancel_during_tool_call_records_history_with_interruption():
    """After barge-in with tool completion, history should contain the
    assistant response AND an interruption note."""
    token = CancelToken()
    runner = AgentRunner(SlowToolStreamingAgent())

    async for event in runner.run_streaming("test", cancel_token=token):
        if event.type == AgentStreamEventType.TOOL_STARTED:
            token.cancel()

    # History: user + assistant + system interruption note
    assert len(runner.history) == 3
    assert runner.history[0] == {"role": "user", "content": "test"}
    assert runner.history[1]["role"] == "assistant"
    assert runner.history[1]["content"] == "Let me check. "
    assert runner.history[2]["role"] == "system"
    assert "interrupted" in runner.history[2]["content"].lower()


@pytest.mark.asyncio
async def test_cancel_during_drain_counts_new_tool_starts():
    """Drain mode should track tool calls that start after cancellation."""
    token = CancelToken()
    runner = AgentRunner(MultiToolDrainAgent())

    events: list[AgentStreamEvent] = []
    async for event in runner.run_streaming("test", cancel_token=token):
        events.append(event)
        if event.type == AgentStreamEventType.TOOL_STARTED and event.call_id == "call_1":
            token.cancel()

    tool_results = [e.call_id for e in events if e.type == AgentStreamEventType.TOOL_RESULT]
    assert tool_results == ["call_1", "call_2"]


@pytest.mark.asyncio
async def test_cancel_without_tool_call_stops_immediately():
    """When cancelled with no tool calls in flight, stream should stop right away."""
    token = CancelToken()
    runner = AgentRunner(SlowStreamingAgent())

    events: list[AgentStreamEvent] = []
    async for event in runner.run_streaming("test", cancel_token=token):
        events.append(event)
        if event.type == AgentStreamEventType.TEXT_DELTA:
            token.cancel()

    text_deltas = [e for e in events if e.type == AgentStreamEventType.TEXT_DELTA]
    assert len(text_deltas) == 1  # Only the first delta
    assert text_deltas[0].text == "Hello "


@pytest.mark.asyncio
async def test_notify_interruption_appends_to_history():
    """notify_interruption should add a system note to the runner's history."""
    runner = AgentRunner(EchoAgent())
    await runner.run("hello")
    assert len(runner.history) == 2

    runner.notify_interruption()
    assert len(runner.history) == 3
    assert runner.history[2]["role"] == "system"
    assert "interrupted" in runner.history[2]["content"].lower()


@pytest.mark.asyncio
async def test_notify_interruption_deduplicates_consecutive_notes():
    """Repeated notifications should not duplicate the same interruption note."""
    runner = AgentRunner(EchoAgent())
    await runner.run("hello")

    runner.notify_interruption()
    runner.notify_interruption()

    interruption_notes = [
        entry
        for entry in runner.history
        if entry["role"] == "system" and "interrupted" in entry["content"].lower()
    ]
    assert len(interruption_notes) == 1
