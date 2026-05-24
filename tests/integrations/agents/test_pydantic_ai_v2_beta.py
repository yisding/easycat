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


class _NoContentToolReturnPart:
    tool_name = "lookup"
    tool_call_id = "tc-none"
    content = None


class OutputToolCallEvent:
    part = _ToolCallPart()


class OutputToolResultEvent:
    def __init__(
        self,
        part: Any | None = None,
        *,
        result: Any = None,
        content: Any = None,
    ) -> None:
        self.part = part or _ToolReturnPart()
        self.result = result
        self.content = content


class FunctionToolResultEvent:
    def __init__(
        self,
        part: Any | None = None,
        *,
        result: Any = None,
        content: Any = None,
    ) -> None:
        self.part = part or _ToolReturnPart()
        self.result = result
        self.content = content


class _EmptyAgentRun:
    output = "done"
    result = None
    ctx = object()

    async def __aenter__(self) -> _EmptyAgentRun:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def __aiter__(self) -> _EmptyAgentRun:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration

    def new_messages(self) -> list[Any]:
        return []


class _LegacyMCPAgent:
    name = "legacy"

    def __init__(self) -> None:
        self.mcp_servers = ["original"]
        self.seen_mcp_servers: list[Any] | None = None

    def iter(
        self,
        text: str,
        *,
        message_history: list[Any] | None = None,
        deps: Any = None,
        model_settings: Any = None,
    ) -> _EmptyAgentRun:
        self.seen_mcp_servers = list(self.mcp_servers)
        return _EmptyAgentRun()


class _GraphStateForSignature:
    _easycat_event_handler: Any = None


class _NoOpGraphRun:
    output = "graph-output"
    history = None

    async def __aenter__(self) -> _NoOpGraphRun:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def __aiter__(self) -> _NoOpGraphRun:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration


class _AmbiguousKeywordGraph:
    """Mimic a v2 graph whose `state` parameter is positional-capable."""

    def __init__(self) -> None:
        self.seen_state: Any = None
        self.seen_deps: Any = None
        self.seen_inputs: Any = None

    def iter(
        self,
        state: Any = None,
        deps: Any = None,
        inputs: Any = None,
    ) -> _NoOpGraphRun:
        self.seen_state = state
        self.seen_deps = deps
        self.seen_inputs = inputs
        return _NoOpGraphRun()


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
    assert result.tool_name == "lookup"
    assert result.call_id == "tc1"
    assert result.result == "{'ok': True}"

    records = [r.data for r in journal.read() if r.name == "tool_phase_changed"]
    assert [(r["phase"], r["tool_name"], r["call_id"]) for r in records] == [
        ("start", "lookup", "tc1"),
        ("result", "lookup", "tc1"),
    ]


def test_v2_function_tool_result_reads_part_content() -> None:
    event = translate_event(FunctionToolResultEvent())

    assert event is not None
    assert event.kind == "tool_result"
    assert event.tool_name == "lookup"
    assert event.call_id == "tc1"
    assert event.result == "{'ok': True}"


@pytest.mark.parametrize("event_cls", [FunctionToolResultEvent, OutputToolResultEvent])
def test_v2_tool_result_with_none_content_is_empty_string(event_cls: type[Any]) -> None:
    journal = InMemoryRingBuffer(capacity=1000)
    rec = _recorder(journal)

    event = translate_event(event_cls(_NoContentToolReturnPart()), rec)

    assert event is not None
    assert event.kind == "tool_result"
    assert event.tool_name == "lookup"
    assert event.call_id == "tc-none"
    assert event.result == ""
    [record] = [r.data for r in journal.read() if r.name == "tool_phase_changed"]
    assert (record["phase"], record["tool_name"], record["call_id"]) == (
        "result",
        "lookup",
        "tc-none",
    )


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


def test_bridge_passes_explicit_v2_toolset_objects_to_agent_kwargs() -> None:
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.toolsets import FunctionToolset

    agent = Agent(TestModel(custom_output_text="done"))
    toolset = FunctionToolset([])
    bridge = PydanticAIBridge(agent=agent, toolsets=[toolset])
    kwargs = bridge._agent_run_kwargs(agent.iter, AgentTurnInput.from_text("hi"))

    assert kwargs["toolsets"] == [toolset]


def test_bridge_converts_mcp_server_uri_strings_to_v2_toolsets() -> None:
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent
    from pydantic_ai.mcp import MCPToolset
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.toolsets import AbstractToolset

    agent = Agent(TestModel(custom_output_text="done"))
    bridge = PydanticAIBridge(agent=agent, mcp_servers=["stdio://server"])
    kwargs = bridge._agent_run_kwargs(agent.iter, AgentTurnInput.from_text("hi"))

    [toolset] = kwargs["toolsets"]
    assert isinstance(toolset, MCPToolset)
    assert isinstance(toolset, AbstractToolset)
    assert not isinstance(toolset, str)
    assert hasattr(toolset, "for_run")


@pytest.mark.asyncio
async def test_bridge_assigns_raw_mcp_servers_to_legacy_agent_attribute() -> None:
    agent = _LegacyMCPAgent()
    bridge = PydanticAIBridge(agent=agent, mcp_servers=["sse://legacy-server"])

    events = [event async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder())]

    assert agent.seen_mcp_servers == ["sse://legacy-server"]
    assert agent.mcp_servers == ["original"]
    assert events[-1].kind == "done"
    assert events[-1].structured_output == "done"


def test_graph_iter_prefers_inputs_keyword_when_state_is_positional_capable() -> None:
    state = _GraphStateForSignature()
    initial_input = object()
    graph = _AmbiguousKeywordGraph()
    deps = object()
    bridge = PydanticAIBridge(
        graph=graph,
        deps=deps,
        state_factory=lambda: state,
        initial_node_factory=lambda text, _state: initial_input,
    )

    graph_run = bridge._graph_iter(initial_input, state)

    assert isinstance(graph_run, _NoOpGraphRun)
    assert graph.seen_state is state
    assert graph.seen_deps is deps
    assert graph.seen_inputs is initial_input


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


@pytest.mark.asyncio
async def test_bridge_invokes_real_v2_graph_keyword_iter_and_drains_final_node_events() -> None:
    pytest.importorskip("pydantic_graph")
    from dataclasses import dataclass, field

    from pydantic_graph import BaseNode, End, GraphBuilder, GraphRunContext
    from pydantic_graph.step import NodeStep

    @dataclass
    class State:
        seen: list[str] = field(default_factory=list)
        _easycat_event_handler: Any = None

    class Start(BaseNode[State, None, str]):
        async def run(self, ctx: GraphRunContext[State, None]) -> Any:
            ctx.state.seen.append("start")
            await ctx.state._easycat_event_handler(PartDeltaEvent(TextPartDelta("start ")))
            return Finish()

    class Finish(BaseNode[State, None, str]):
        async def run(self, ctx: GraphRunContext[State, None]) -> Any:
            ctx.state.seen.append("finish")
            await ctx.state._easycat_event_handler(PartDeltaEvent(TextPartDelta("finish")))
            return End("graph-output")

    builder = GraphBuilder(
        state_type=State,
        deps_type=type(None),
        input_type=Start,
        output_type=str,
    )
    start_step = NodeStep(Start)
    finish_step = NodeStep(Finish)
    builder.add(builder.edge_from(builder.start_node).to(start_step))
    builder.add(builder.edge_from(start_step).to(finish_step))
    builder.add(builder.edge_from(finish_step).to(builder.end_node))
    graph = builder.build(validate_graph_structure=False)
    state = State()
    bridge = PydanticAIBridge(
        graph=graph,
        state_factory=lambda: state,
        initial_node_factory=lambda text, _state: Start(),
    )
    journal = InMemoryRingBuffer(capacity=1000)

    events = [
        event async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder(journal))
    ]

    assert state.seen == ["start", "finish"]
    assert [(event.kind, event.text) for event in events] == [
        ("text_delta", "start "),
        ("text_delta", "finish"),
        ("done", "start finish"),
    ]
    assert events[-1].structured_output == "graph-output"

    entered = [
        record.data["display_name"] for record in journal.read() if record.name == "unit_entered"
    ]
    assert entered == ["Start", "Finish"]
