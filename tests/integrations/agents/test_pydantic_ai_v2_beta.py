from __future__ import annotations

from typing import Any

import pytest

from easycat.integrations.agents._pydantic_ai_events import translate_event
from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import AgentTurnInput, BridgeInputError, RecorderContext
from easycat.integrations.agents.pydantic_ai import PydanticAIBridge, _GraphEventHandler
from easycat.runtime.journal import InMemoryRingBuffer


class TextPartDelta:
    def __init__(self, content_delta: str) -> None:
        self.content_delta = content_delta


class ToolCallPartDelta:
    def __init__(self, args_delta: Any) -> None:
        self.args_delta = args_delta


class PartDeltaEvent:
    def __init__(self, delta: Any) -> None:
        self.delta = delta


class _ToolCallPart:
    tool_name = "lookup"
    tool_call_id = "tc1"


class _ToolReturnPart:
    tool_name = "lookup"
    tool_call_id = "tc1"
    content = {"ok": True}


class OutputToolCallEvent:
    part = _ToolCallPart()


class OutputToolResultEvent:
    part = _ToolReturnPart()


class FunctionToolResultEvent:
    part = _ToolReturnPart()


class FinalResultEvent:
    tool_name = "final"
    tool_call_id = "tc-final"


def _recorder(journal: InMemoryRingBuffer | None = None) -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


def test_v2_output_tool_events_translate_to_tool_phases() -> None:
    journal = InMemoryRingBuffer(capacity=1000)
    rec = _recorder(journal)

    started = translate_event(OutputToolCallEvent(), rec)
    result = translate_event(OutputToolResultEvent(), rec)

    assert started is not None
    assert started.kind == "tool_started"
    assert started.tool_name == "lookup"
    assert started.call_id == "tc1"
    assert result is not None
    assert result.kind == "tool_result"
    assert result.call_id == "tc1"
    assert result.result == "{'ok': True}"

    phases = [r.data["phase"] for r in journal.read() if r.name == "tool_phase_changed"]
    assert phases == ["start", "result"]


def test_v2_function_tool_result_reads_part_content() -> None:
    event = translate_event(FunctionToolResultEvent())

    assert event is not None
    assert event.kind == "tool_result"
    assert event.call_id == "tc1"
    assert event.result == "{'ok': True}"


def test_v2_final_result_without_output_is_not_done_event() -> None:
    assert translate_event(FinalResultEvent()) is None


def test_tool_call_delta_dict_is_serialized_as_text() -> None:
    event = translate_event(PartDeltaEvent(ToolCallPartDelta({"city": "Tokyo"})))

    assert event is not None
    assert event.kind == "tool_delta"
    assert event.text == '{"city": "Tokyo"}'


@pytest.mark.asyncio
async def test_graph_event_handler_accepts_v2_stream_signature() -> None:
    handler = _GraphEventHandler(_recorder())

    async def events():
        yield PartDeltaEvent(TextPartDelta("hello"))

    await handler(object(), events())

    drained = handler.drain()
    assert handler.was_called
    assert [event.kind for event in drained] == ["text_delta"]
    assert handler.accumulated_text == "hello"


class _MockAgentRun:
    output = "done"
    result = None
    ctx = object()

    async def __aenter__(self) -> _MockAgentRun:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def __aiter__(self) -> _MockAgentRun:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration

    def new_messages(self) -> list[Any]:
        return []


class _V2StyleAgent:
    name = "v2-agent"

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def iter(
        self,
        text: str,
        *,
        message_history: list[Any] | None = None,
        deps: Any = None,
        model_settings: Any = None,
        toolsets: list[Any] | None = None,
    ) -> _MockAgentRun:
        self.kwargs = {
            "text": text,
            "message_history": message_history,
            "deps": deps,
            "model_settings": model_settings,
            "toolsets": toolsets,
        }
        return _MockAgentRun()


@pytest.mark.asyncio
async def test_bridge_passes_mcp_servers_as_v2_toolsets() -> None:
    agent = _V2StyleAgent()
    bridge = PydanticAIBridge(agent=agent, mcp_servers=["stdio://server"])

    events = [event async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder())]

    assert agent.kwargs["toolsets"] == ["stdio://server"]
    assert [event.kind for event in events] == ["done"]
    assert events[0].structured_output == "done"


def test_bridge_rejects_mcp_servers_and_toolsets_together() -> None:
    with pytest.raises(BridgeInputError, match="either mcp_servers= or toolsets="):
        PydanticAIBridge(agent=object(), mcp_servers=["stdio://server"], toolsets=[object()])


@pytest.mark.asyncio
async def test_bridge_invokes_real_v2_test_model_when_extra_installed() -> None:
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    agent = Agent(TestModel(custom_output_text="hello from v2"))
    bridge = PydanticAIBridge(agent=agent)

    events = [event async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder())]

    assert [(event.kind, event.text) for event in events] == [
        ("text_delta", "hello "),
        ("text_delta", "from "),
        ("text_delta", "v2"),
        ("done", "hello from v2"),
    ]
    assert events[-1].structured_output == "hello from v2"
