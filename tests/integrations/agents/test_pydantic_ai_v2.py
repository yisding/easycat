from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Self

import pytest

from easycat.integrations.agents._pydantic_ai_events import translate_event
from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents._text_stream import AgentTextStream
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentTurnInput,
    BridgeInputError,
    RecorderContext,
)
from easycat.integrations.agents.pydantic_ai import PydanticAIBridge, _GraphEventHandler
from easycat.runtime import InMemoryRingBuffer


def test_graph_snapshot_state_is_secret_safe_without_opaque_repr() -> None:
    class _Opaque:
        def __str__(self) -> str:
            return "OPAQUE_STATE_SECRET_LEAK"

    @dataclass
    class _State:
        api_key: str = "PYDANTIC_STATE_SECRET_LEAK"
        auth: str = "PYDANTIC_AUTH_SECRET_LEAK"
        author: str = "Ada"
        apiVersion: str = "v1"
        keyboardLayout: str = "dvorak"
        safe: str = "ok"
        opaque: Any = field(default_factory=_Opaque)

    bridge = object.__new__(PydanticAIBridge)
    bridge._mode = "graph"
    bridge._graph = None
    bridge._active_node = None
    bridge._graph_states = {}
    bridge._state = _State()

    fields = bridge.snapshot_state().fields
    serialized = json.dumps(fields)

    assert fields["state"] == {
        "author": "Ada",
        "apiVersion": "v1",
        "keyboardLayout": "dvorak",
        "safe": "ok",
        "opaque": "[UNSERIALIZABLE]",
    }
    assert "PYDANTIC_STATE_SECRET_LEAK" not in serialized
    assert "PYDANTIC_AUTH_SECRET_LEAK" not in serialized
    assert "OPAQUE_STATE_SECRET_LEAK" not in serialized


class TextPartDelta:
    def __init__(self, content_delta: str) -> None:
        self.content_delta = content_delta


class TextPart:
    def __init__(self, content: str) -> None:
        self.content = content


class PartStartEvent:
    def __init__(self, part: Any, *, index: int = 0) -> None:
        self.part = part
        self.index = index


class ToolCallPartDelta:
    def __init__(self, args_delta: Any, tool_call_id: str | None = None) -> None:
        self.args_delta = args_delta
        self.tool_call_id = tool_call_id


class PartDeltaEvent:
    def __init__(self, delta: Any, *, index: int = 0) -> None:
        self.delta = delta
        self.index = index


class _ToolCallPart:
    tool_name = "lookup"
    tool_call_id = "tc1"


class _ToolReturnPart:
    tool_name = "lookup"
    tool_call_id = "tc1"
    content = {"ok": True}  # noqa: RUF012 test fake uses shared class fixture


class _NoContentToolReturnPart:
    tool_name = "lookup"
    tool_call_id = "tc-none"
    content = None


class OutputToolCallEvent:
    part = _ToolCallPart()


class FunctionToolCallEvent:
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

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def __aiter__(self) -> _EmptyAgentRun:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration

    def new_messages(self) -> list[Any]:
        return []


class _StructuredOutputRun(_EmptyAgentRun):
    output = {"utterance": "hello", "intent": "capture"}  # noqa: RUF012 test fake uses shared class fixture


class _UsageAgentRun(_EmptyAgentRun):
    def usage(self) -> dict[str, int]:
        return {
            "request_tokens": 23,
            "response_tokens": 9,
            "cache_read_tokens": 6,
        }


class _UsageAgent:
    name = "usage"
    model = "test-model"

    def iter(
        self,
        text: str,
        *,
        message_history: list[Any] | None = None,
        deps: Any = None,
        model_settings: Any = None,
    ) -> _UsageAgentRun:
        return _UsageAgentRun()


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


class _StructuredOutputAgent:
    def iter(
        self,
        text: str,
        *,
        message_history: list[Any] | None = None,
        deps: Any = None,
        model_settings: Any = None,
    ) -> _StructuredOutputRun:
        return _StructuredOutputRun()


class _GraphStateForSignature:
    _easycat_event_handler: Any = None


class _NoOpGraphRun:
    output = "graph-output"
    history = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def __aiter__(self) -> _NoOpGraphRun:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration


class _StructuredOutputGraphRun(_NoOpGraphRun):
    output = {"utterance": "hello", "intent": "capture"}  # noqa: RUF012 test fake uses shared class fixture


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


class _StructuredOutputGraph(_AmbiguousKeywordGraph):
    def iter(
        self,
        state: Any = None,
        deps: Any = None,
        inputs: Any = None,
    ) -> _StructuredOutputGraphRun:
        self.seen_state = state
        self.seen_deps = deps
        self.seen_inputs = inputs
        return _StructuredOutputGraphRun()


class _SequenceOutputGraph(_AmbiguousKeywordGraph):
    def __init__(self, outputs: list[Any]) -> None:
        super().__init__()
        self.outputs = iter(outputs)

    def iter(
        self,
        state: Any = None,
        deps: Any = None,
        inputs: Any = None,
    ) -> _NoOpGraphRun:
        self.seen_state = state
        self.seen_deps = deps
        self.seen_inputs = inputs
        run = _NoOpGraphRun()
        run.output = next(self.outputs)
        return run


class FinalResultEvent:
    tool_name = "final"
    tool_call_id = "tc-final"


def _recorder(journal: InMemoryRingBuffer | None = None) -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


@pytest.mark.parametrize(
    ("call_event_cls", "result_event_cls"),
    [
        (FunctionToolCallEvent, FunctionToolResultEvent),
        (OutputToolCallEvent, OutputToolResultEvent),
    ],
)
def test_v2_tool_events_translate_to_tool_phases(
    call_event_cls: type[Any],
    result_event_cls: type[Any],
) -> None:
    journal = InMemoryRingBuffer(capacity=1000)
    rec = _recorder(journal)

    started = translate_event(call_event_cls(), rec)
    result = translate_event(result_event_cls(), rec)

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


def test_text_part_start_preserves_the_initial_stream_content() -> None:
    event = translate_event(PartStartEvent(TextPart("Loverboy")))

    assert event is not None
    assert event.kind == "text_replace"
    assert event.text == "Loverboy"
    assert event.part_index == 0


def test_empty_text_part_start_can_clear_an_existing_part() -> None:
    event = translate_event(PartStartEvent(TextPart(""), index=3))

    assert event == AgentBridgeEvent(kind="text_replace", text="", part_index=3)


def test_same_index_text_part_start_replaces_instead_of_appending() -> None:
    stream = AgentTextStream()
    first = translate_event(PartStartEvent(TextPart("stale"), index=2))
    replacement = translate_event(PartStartEvent(TextPart("correct"), index=2))

    assert first is not None
    assert replacement is not None
    stream.apply(first)
    update = stream.apply(replacement)

    assert update is not None
    assert update.operation == "replace"
    assert update.text == "correct"
    assert update.appended_text is None


def test_real_v2_text_part_start_preserves_the_initial_stream_content() -> None:
    pytest.importorskip("pydantic_ai")
    from pydantic_ai.messages import PartStartEvent as RealPartStartEvent
    from pydantic_ai.messages import TextPart as RealTextPart

    event = translate_event(RealPartStartEvent(index=0, part=RealTextPart(content="Loverboy")))

    assert event is not None
    assert event.kind == "text_replace"
    assert event.text == "Loverboy"
    assert event.part_index == 0


def test_tool_call_delta_dict_is_serialized_as_text() -> None:
    event = translate_event(PartDeltaEvent(ToolCallPartDelta({"city": "Tokyo"})))

    assert event is not None
    assert event.kind == "tool_delta"
    assert event.text == '{"city": "Tokyo"}'
    assert event.call_id == ""


def test_tool_call_delta_preserves_call_id_in_event_and_recorder() -> None:
    journal = InMemoryRingBuffer(capacity=1000)
    recorder = _recorder(journal)

    event = translate_event(
        PartDeltaEvent(ToolCallPartDelta('{"city":', tool_call_id="tc-delta")),
        recorder,
    )

    assert event is not None
    assert event.kind == "tool_delta"
    assert event.text == '{"city":'
    assert event.call_id == "tc-delta"
    [record] = [r.data for r in journal.read() if r.name == "tool_phase_changed"]
    assert (record["phase"], record["tool_name"], record["call_id"]) == (
        "delta",
        "",
        "tc-delta",
    )


def test_tool_call_delta_reuses_call_id_for_argument_continuations() -> None:
    journal = InMemoryRingBuffer(capacity=1000)
    recorder = _recorder(journal)
    tool_call_ids: dict[int, str] = {}

    initial = translate_event(
        PartDeltaEvent(ToolCallPartDelta("", tool_call_id="tc-split"), index=4),
        recorder,
        tool_call_ids=tool_call_ids,
    )
    continuation = translate_event(
        PartDeltaEvent(ToolCallPartDelta('{"city":'), index=4),
        recorder,
        tool_call_ids=tool_call_ids,
    )

    assert initial is None
    assert continuation is not None
    assert continuation.kind == "tool_delta"
    assert continuation.call_id == "tc-split"
    [record] = [r.data for r in journal.read() if r.name == "tool_phase_changed"]
    assert record["call_id"] == "tc-split"


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


@pytest.mark.asyncio
async def test_graph_event_handler_accumulates_same_index_replacement() -> None:
    handler = _GraphEventHandler(_recorder())

    async def events():
        yield PartStartEvent(TextPart("stale"), index=1)
        yield PartStartEvent(TextPart("correct"), index=1)
        yield PartDeltaEvent(TextPartDelta(" result"), index=1)

    await handler(object(), events())

    assert [event.kind for event in handler.drain()] == [
        "text_replace",
        "text_replace",
        "text_delta",
    ]
    assert handler.accumulated_text == "correct result"


@pytest.mark.asyncio
async def test_graph_event_handler_namespaces_part_indexes_per_agent_stream() -> None:
    handler = _GraphEventHandler(_recorder())

    async def first_stream():
        yield PartStartEvent(TextPart("first "), index=0)

    async def second_stream():
        yield PartStartEvent(TextPart("second"), index=0)

    await handler(object(), first_stream())
    await handler(object(), second_stream())

    events = handler.drain()
    assert [event.part_index for event in events] == [0, 1]
    assert handler.accumulated_text == "first second"


@pytest.mark.asyncio
async def test_graph_event_handler_reserves_unique_indexes_for_concurrent_streams() -> None:
    handler = _GraphEventHandler(_recorder())
    second_started = asyncio.Event()

    async def first_stream():
        yield PartStartEvent(TextPart("first"), index=0)
        # Keep this stream open until the second stream has claimed its own
        # namespace, so both streams observe the pre-update allocator state.
        await second_started.wait()
        yield PartDeltaEvent(TextPartDelta(" one"), index=0)

    async def second_stream():
        yield PartStartEvent(TextPart("second"), index=0)
        second_started.set()
        yield PartDeltaEvent(TextPartDelta(" two"), index=0)

    await asyncio.gather(
        handler(object(), first_stream()),
        handler(object(), second_stream()),
    )

    events = handler.drain()
    indexes_by_stream_text = {event.text: event.part_index for event in events}
    assert indexes_by_stream_text["first"] != indexes_by_stream_text["second"]
    assert indexes_by_stream_text[" one"] == indexes_by_stream_text["first"]
    assert indexes_by_stream_text[" two"] == indexes_by_stream_text["second"]
    assert handler.accumulated_text == "first onesecond two"


@pytest.mark.asyncio
async def test_graph_event_handler_v1_events_append_new_parts_per_start() -> None:
    handler = _GraphEventHandler(_recorder())

    # v1 single events carry no stream identity, so a repeated local index 0
    # from a second agent must claim a fresh part instead of replacing the
    # first agent's text; deltas follow their local index's latest part.
    await handler(PartStartEvent(TextPart("first"), index=0))
    await handler(PartDeltaEvent(TextPartDelta(" one"), index=0))
    await handler(PartStartEvent(TextPart("second"), index=0))
    await handler(PartDeltaEvent(TextPartDelta(" two"), index=0))

    events = handler.drain()
    assert [(event.kind, event.text, event.part_index) for event in events] == [
        ("text_replace", "first", 0),
        ("text_delta", " one", 0),
        ("text_replace", "second", 1),
        ("text_delta", " two", 1),
    ]
    assert handler.accumulated_text == "first onesecond two"


@pytest.mark.asyncio
async def test_graph_event_handler_v1_and_v2_events_share_one_part_allocator() -> None:
    handler = _GraphEventHandler(_recorder())

    async def v2_stream():
        yield PartStartEvent(TextPart("v2"), index=0)

    await handler(object(), v2_stream())
    await handler(PartStartEvent(TextPart("v1"), index=0))

    events = handler.drain()
    assert [(event.text, event.part_index) for event in events] == [("v2", 0), ("v1", 1)]
    assert handler.accumulated_text == "v2v1"


class _NodeEventStream:
    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Any:
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class _StreamingNode:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def stream(self, _ctx: object) -> _NodeEventStream:
        return _NodeEventStream(self._events)


class _FakeIterRun:
    def __init__(self, nodes: list[Any]) -> None:
        self._nodes = list(nodes)
        self.ctx = object()
        self.output = None
        self.result = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Any:
        if not self._nodes:
            raise StopAsyncIteration
        return self._nodes.pop(0)

    def new_messages(self) -> list[Any]:
        return []


class _FakeIterAgent:
    name = "fake-iter-agent"

    def __init__(self, nodes: list[Any]) -> None:
        self._nodes = nodes

    def iter(self, _text: str, **_kwargs: Any) -> _FakeIterRun:
        return _FakeIterRun(self._nodes)


@pytest.mark.asyncio
async def test_iter_mode_namespaces_part_indexes_across_model_request_nodes() -> None:
    # Each model-request node streams one response whose part indexes restart
    # at 0: a same-index restart inside one node stays a replacement, while
    # the next node's text must land in a fresh part instead of clobbering
    # already-streamed (possibly spoken) text.
    first_node = _StreamingNode(
        [
            PartStartEvent(TextPart("stale"), index=0),
            PartStartEvent(TextPart("Let me check."), index=0),
        ]
    )
    second_node = _StreamingNode(
        [
            PartStartEvent(TextPart("It is sunny."), index=0),
            PartDeltaEvent(TextPartDelta(" Enjoy."), index=0),
        ]
    )
    bridge = PydanticAIBridge(agent=_FakeIterAgent([first_node, second_node]))

    events = [event async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder())]

    assert [(event.kind, event.text, event.part_index) for event in events] == [
        ("text_replace", "stale", 0),
        ("text_replace", "Let me check.", 0),
        ("text_replace", "It is sunny.", 1),
        ("text_delta", " Enjoy.", 1),
        ("done", "Let me check.It is sunny. Enjoy.", None),
    ]


@pytest.mark.asyncio
async def test_graph_event_handler_records_context_usage_and_model() -> None:
    journal = InMemoryRingBuffer(capacity=1000)
    handler = _GraphEventHandler(_recorder(journal))
    ctx = type(
        "GraphRunContext",
        (),
        {
            "usage": {"request_tokens": 11, "response_tokens": 4},
            "model": "graph-model",
        },
    )()

    async def events():
        if False:
            yield None

    await handler(ctx, events())

    [usage] = [record for record in journal.read() if record.name == "agent_usage"]
    assert usage.data["model"] == "graph-model"
    assert usage.data["input_tokens"] == 11
    assert usage.data["output_tokens"] == 4


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
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.toolsets import AbstractToolset

    mcp_mod = pytest.importorskip("pydantic_ai.mcp")
    MCPToolset = getattr(mcp_mod, "MCPToolset", None)
    if MCPToolset is None:
        pytest.skip("PydanticAI MCPToolset requires a newer PydanticAI v1 or v2")

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


@pytest.mark.asyncio
async def test_bridge_speaks_structured_output_when_no_text_parts_stream() -> None:
    bridge = PydanticAIBridge(agent=_StructuredOutputAgent())

    events = [event async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder())]

    assert [(event.kind, event.text) for event in events] == [
        ("done", "{'utterance': 'hello', 'intent': 'capture'}")
    ]
    assert events[-1].structured_output == {"utterance": "hello", "intent": "capture"}


@pytest.mark.asyncio
async def test_graph_bridge_speaks_structured_output_when_no_text_parts_stream() -> None:
    state = _GraphStateForSignature()
    bridge = PydanticAIBridge(
        graph=_StructuredOutputGraph(),
        state_factory=lambda: state,
        initial_node_factory=lambda text, _state: text,
    )

    events = [event async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder())]

    assert [(event.kind, event.text) for event in events] == [
        ("done", "{'utterance': 'hello', 'intent': 'capture'}")
    ]
    assert events[-1].structured_output == {"utterance": "hello", "intent": "capture"}


@pytest.mark.asyncio
async def test_graph_bridge_does_not_replay_prior_session_output() -> None:
    bridge = PydanticAIBridge(
        graph=_SequenceOutputGraph([{"utterance": "first"}, None]),
        state_factory=_GraphStateForSignature,
        initial_node_factory=lambda text, _state: text,
    )
    first_recorder = _recorder()
    second_recorder = JournalAgentRecorder(
        journal=InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r2", session_id="s2", turn_id="t2"),
    )

    first = [
        event
        async for event in bridge.invoke(
            AgentTurnInput.from_text("first"),
            first_recorder,
        )
    ]
    second = [
        event
        async for event in bridge.invoke(
            AgentTurnInput.from_text("second"),
            second_recorder,
        )
    ]

    assert first[-1].structured_output == {"utterance": "first"}
    assert second[-1].text == ""
    assert second[-1].structured_output is None


@pytest.mark.asyncio
async def test_bridge_records_v2_usage_accessor() -> None:
    journal = InMemoryRingBuffer(capacity=1000)
    bridge = PydanticAIBridge(agent=_UsageAgent())

    _ = [
        event
        async for event in bridge.invoke(
            AgentTurnInput.from_text("hi"),
            _recorder(journal),
        )
    ]

    [usage] = [record for record in journal.read() if record.name == "agent_usage"]
    assert usage.data == {
        "run_id": "r1",
        "provider": "pydantic_ai",
        "model": "test-model",
        "input_tokens": 23,
        "output_tokens": 9,
        "cached_input_tokens": 6,
    }


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

    assert [(event.kind, event.text, event.part_index) for event in events] == [
        ("text_replace", "", 0),
        ("text_delta", "hello ", 0),
        ("text_delta", "from ", 0),
        ("text_delta", "v2", 0),
        ("done", "hello from v2", None),
    ]
    assert events[-1].structured_output == "hello from v2"


@pytest.mark.asyncio
async def test_bridge_invokes_real_v2_graph_keyword_iter_and_drains_final_node_events() -> None:
    pydantic_graph = pytest.importorskip("pydantic_graph")
    if not hasattr(pydantic_graph, "GraphBuilder"):
        pytest.skip("GraphBuilder requires PydanticGraph v2")
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
