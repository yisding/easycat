"""Tests for PydanticAIAdapter.

Uses lightweight mock objects that replicate PydanticAI's run/run_stream API
surface so the tests run without pydantic-ai installed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from easycat.agent_runner import AgentStreamEventType
from easycat.agents.pydantic_ai import PydanticAIAdapter
from easycat.cancel import CancelToken

# ── Mock PydanticAI objects ───────────────────────────────────────


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


# ── Streaming run_streaming() tests ──────────────────────────────


@pytest.mark.asyncio
async def test_streaming_yields_text_deltas():
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
async def test_streaming_yields_done_event():
    agent = MockPydanticAgent(stream_chunks=[["Hi", " there"]])
    adapter = PydanticAIAdapter(agent)

    events = []
    async for event in adapter.run_streaming("greet"):
        events.append(event)

    done_events = [e for e in events if e.type == AgentStreamEventType.DONE]
    assert len(done_events) == 1
    assert done_events[0].text == "Hi there"


@pytest.mark.asyncio
async def test_streaming_updates_message_history():
    agent = MockPydanticAgent(
        stream_chunks=[["First", " response"], ["Second", " response"]]
    )
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
async def test_streaming_first_call_no_history():
    agent = MockPydanticAgent(stream_chunks=[["reply"]])
    adapter = PydanticAIAdapter(agent)

    async for _ in adapter.run_streaming("hello"):
        pass
    assert agent.stream_calls[0]["message_history"] is None


@pytest.mark.asyncio
async def test_streaming_passes_deps():
    agent = MockPydanticAgent(stream_chunks=[["ok"]])
    adapter = PydanticAIAdapter(agent, deps="my_deps")

    async for _ in adapter.run_streaming("query"):
        pass
    assert agent.stream_calls[0]["deps"] == "my_deps"


@pytest.mark.asyncio
async def test_streaming_passes_model_settings():
    agent = MockPydanticAgent(stream_chunks=[["ok"]])
    settings = {"max_tokens": 100}
    adapter = PydanticAIAdapter(agent, model_settings=settings)

    async for _ in adapter.run_streaming("query"):
        pass
    assert agent.stream_calls[0]["model_settings"] == settings


# ── Cancellation tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_respects_cancel_token():
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
async def test_streaming_cancel_still_emits_done():
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
