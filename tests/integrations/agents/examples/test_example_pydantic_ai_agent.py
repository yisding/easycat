"""Example 1: PydanticAIBridge wrapping a plain pydantic_ai.Agent.

Mirrors plan appendix Example 1 — single agent with tools, zero custom
bridge code.  Uses duck-typed mocks so the real ``pydantic_ai`` SDK is
not required.

This fixture runs end-to-end using mock objects.
"""

from __future__ import annotations

from typing import Any

import pytest

from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import AgentTurnInput, RecorderContext
from easycat.integrations.agents.pydantic_ai import PydanticAIBridge
from easycat.runtime.journal import InMemoryRingBuffer

# ── Mock PydanticAI objects ──────────────────────────────────────


class TextPartDelta:
    """Duck-types as ``pydantic_ai.messages.TextPartDelta``."""

    def __init__(self, content_delta: str) -> None:
        self.content_delta = content_delta


class ToolCallPartDelta:
    def __init__(self, args_delta: str) -> None:
        self.args_delta = args_delta


class PartDeltaEvent:
    """Duck-types as ``pydantic_ai.agent.PartDeltaEvent``."""

    def __init__(self, delta: Any) -> None:
        self.delta = delta


class _ToolPart:
    def __init__(self, tool_name: str, tool_call_id: str = "tc1") -> None:
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id


class FunctionToolCallEvent:
    def __init__(self, tool_name: str, tool_call_id: str = "tc1") -> None:
        self.part = _ToolPart(tool_name, tool_call_id)


class FunctionToolResultEvent:
    def __init__(self, tool_call_id: str = "tc1", result: str = "24°C and sunny") -> None:
        self.tool_call_id = tool_call_id
        self.result = result


class _MockNodeStream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def __aenter__(self) -> _MockNodeStream:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def __aiter__(self) -> _MockNodeStream:
        self._iter = iter(self._events)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


def _MockNode(name: str, stream_events: list[Any] | None = None) -> Any:
    events = stream_events or []

    class _Node:
        def stream(self, ctx: Any) -> _MockNodeStream:
            return _MockNodeStream(events)

    _Node.__name__ = name
    _Node.__qualname__ = name
    return _Node()


class _MockAgentRun:
    def __init__(self, nodes: list[Any], output: Any = None) -> None:
        self._nodes = nodes
        self.output = output
        self.result = None
        self.ctx = type("Ctx", (), {})()

    async def __aenter__(self) -> _MockAgentRun:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def __aiter__(self) -> _MockAgentRun:
        self._iter = iter(self._nodes)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration

    def new_messages(self) -> list[Any]:
        return []


class _MockPydanticAgent:
    """Duck-types as ``pydantic_ai.Agent``."""

    def __init__(self, name: str, nodes: list[Any], output: Any = None) -> None:
        self.name = name
        self._run = _MockAgentRun(nodes, output=output)

    def iter(self, text: str, **kwargs: Any) -> _MockAgentRun:
        return self._run


# ── Tests ────────────────────────────────────────────────────────


def _recorder(journal: InMemoryRingBuffer | None = None) -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


class TestPydanticAIAgentExample:
    """Plan appendix Example 1 — PydanticAIBridge Agent mode."""

    @pytest.mark.asyncio
    async def test_invoke_streams_text_deltas_and_done(self):
        """Weather agent streams text and completes with a done event."""
        nodes = [
            _MockNode(
                "ModelRequestNode",
                stream_events=[
                    PartDeltaEvent(TextPartDelta("The forecast ")),
                    PartDeltaEvent(TextPartDelta("is 24°C and sunny.")),
                ],
            ),
            _MockNode(
                "CallToolsNode",
                stream_events=[
                    FunctionToolCallEvent("weather_forecast"),
                    FunctionToolResultEvent("tc1", "24°C and sunny"),
                ],
            ),
            _MockNode(
                "ModelRequestNode",
                stream_events=[
                    PartDeltaEvent(TextPartDelta("It will be warm.")),
                ],
            ),
        ]
        agent = _MockPydanticAgent("WeatherAgent", nodes, output="It will be warm.")
        bridge = PydanticAIBridge(agent=agent)

        rec = _recorder()
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("forecast for Tokyo"), rec):
            events.append(ev)

        text_events = [e for e in events if e.kind == "text_delta"]
        done_events = [e for e in events if e.kind == "done"]
        assert len(text_events) >= 2
        assert len(done_events) == 1

    @pytest.mark.asyncio
    async def test_journal_records_tool_call_phases(self):
        """Tool calls produce tool_phase_changed records in the journal."""
        nodes = [
            _MockNode(
                "CallToolsNode",
                stream_events=[
                    FunctionToolCallEvent("weather_forecast"),
                    FunctionToolResultEvent("tc1", "24°C"),
                ],
            ),
        ]
        agent = _MockPydanticAgent("WeatherAgent", nodes, output="24°C")
        bridge = PydanticAIBridge(agent=agent)

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        async for _ in bridge.invoke(AgentTurnInput.from_text("weather?"), rec):
            pass

        records = journal.read()
        tool_records = [r for r in records if r.name == "tool_phase_changed"]
        assert len(tool_records) >= 2
        phases = [r.data["phase"] for r in tool_records]
        assert "start" in phases
        assert "result" in phases

    @pytest.mark.asyncio
    async def test_journal_has_agent_cursor_pair(self):
        nodes = [
            _MockNode(
                "ModelRequestNode",
                stream_events=[PartDeltaEvent(TextPartDelta("hi"))],
            ),
        ]
        agent = _MockPydanticAgent("TestAgent", nodes, output="hi")
        bridge = PydanticAIBridge(agent=agent)

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        async for _ in bridge.invoke(AgentTurnInput.from_text("hello"), rec):
            pass

        records = journal.read()
        names = [r.name for r in records]
        assert names[0] == "unit_entered"
        assert names[-1] == "unit_exited"

    def test_snapshot_state(self):
        agent = _MockPydanticAgent("WeatherAgent", [])
        bridge = PydanticAIBridge(agent=agent)
        snap = bridge.snapshot_state()
        assert snap.kind == "pydantic_ai_agent"

    def test_committable_boundaries_published(self):
        assert PydanticAIBridge.COMMITTABLE_BOUNDARIES
