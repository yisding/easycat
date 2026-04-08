"""Bridge-level transition record tests.

AC2.5 + AC2.17: OpenAI Agents handoff triple
AC2.6:          PydanticAI Agent mode node transitions
AC2.6a-b:       PydanticAI Graph mode transitions + history artifact
AC2.6c:         PydanticAI snapshot 4KB overflow via bridge
AC2.6d:         Runtime ConventionViolationError (graph handler unused)
AC2.8:          Committable flag state machine
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import (
    AgentTurnInput,
    ConventionViolationError,
    RecorderContext,
)
from easycat.runtime.journal import InMemoryRingBuffer


def _make_recorder(journal: InMemoryRingBuffer | None = None) -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


# ════════════════════════════════════════════════════════════════════
#  Mock objects for OpenAI Agents SDK
# ════════════════════════════════════════════════════════════════════


class _MockAgent:
    def __init__(self, name: str = "AgentA") -> None:
        self.name = name


class _MockRawResponseEvent:
    """Simulates ``response.output_text.delta``."""

    def __init__(self, delta: str) -> None:
        self.type = "response.output_text.delta"
        self.delta = delta


class _MockStreamEvent:
    def __init__(self, *, type: str, data: Any = None, item: Any = None) -> None:
        self.type = type
        self.data = data
        self.item = item


class _MockRunResult:
    """Simulates the result of ``Runner.run_streamed()``."""

    def __init__(
        self,
        events: list[_MockStreamEvent],
        *,
        last_agent: _MockAgent | None = None,
        message_history: list[dict[str, str]] | None = None,
    ) -> None:
        self._events = events
        self.last_agent = last_agent
        self.last_response_id = "resp-123"
        self.final_output = "hello"
        self._message_history = message_history or []

    async def stream_events(self) -> AsyncIterator[_MockStreamEvent]:
        for ev in self._events:
            yield ev

    def to_input_list(self) -> list[dict[str, str]]:
        return self._message_history


class _MockRunner:
    """Replacement for ``agents.Runner``."""

    def __init__(self, result: _MockRunResult) -> None:
        self._result = result

    def run_streamed(self, agent: Any, input_data: Any, **kwargs: Any) -> _MockRunResult:
        return self._result


# ════════════════════════════════════════════════════════════════════
#  Mock objects for PydanticAI
# ════════════════════════════════════════════════════════════════════


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
    def __init__(self, tool_call_id: str = "tc1", result: str = "42") -> None:
        self.tool_call_id = tool_call_id
        self.result = result


class _MockNodeStream:
    """Async context manager returned by ``node.stream(ctx)``."""

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
    """Create a duck-typed PydanticAI iter node whose type().__name__ == name."""
    events = stream_events or []

    class _Node:
        def stream(self, ctx: Any) -> _MockNodeStream:
            return _MockNodeStream(events)

    _Node.__name__ = name
    _Node.__qualname__ = name
    return _Node()


class _MockAgentRunCtx:
    pass


class _MockAgentRun:
    """Async context manager and async iterator for ``agent.iter()``."""

    def __init__(
        self,
        nodes: list[_MockNode],
        messages: list[Any] | None = None,
        output: Any = None,
    ) -> None:
        self._nodes = nodes
        self._messages = messages or []
        self.output = output
        self.result = None
        self.ctx = _MockAgentRunCtx()

    async def __aenter__(self) -> _MockAgentRun:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def __aiter__(self) -> _MockAgentRun:
        self._iter = iter(self._nodes)
        return self

    async def __anext__(self) -> _MockNode:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration

    def new_messages(self) -> list[Any]:
        return self._messages


class _MockPydanticAgent:
    """Duck-types as ``pydantic_ai.Agent``."""

    def __init__(self, name: str, nodes: list[_MockNode], output: Any = None) -> None:
        self.name = name
        self._run = _MockAgentRun(nodes, output=output)

    def iter(self, text: str, **kwargs: Any) -> _MockAgentRun:
        return self._run


# ════════════════════════════════════════════════════════════════════
#  Mock objects for PydanticAI Graph mode
# ════════════════════════════════════════════════════════════════════


class _MockGraphNode:
    """A graph node whose class name is set dynamically."""

    def __init__(self, name: str) -> None:
        # Create a new type so type(node).__name__ returns the expected name.
        self.__class__ = type(name, (), {})  # type: ignore[assignment]


class _MockGraphRun:
    """Async context manager + async iterator for ``graph.iter()``."""

    def __init__(
        self,
        nodes: list[_MockGraphNode],
        result: Any = None,
        history: list[Any] | None = None,
    ) -> None:
        self._nodes = nodes
        self.result = result
        self.history = history

    async def __aenter__(self) -> _MockGraphRun:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def __aiter__(self) -> _MockGraphRun:
        self._iter = iter(self._nodes)
        return self

    async def __anext__(self) -> _MockGraphNode:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _MockGraph:
    def __init__(
        self,
        nodes: list[_MockGraphNode],
        result: Any = None,
        history: list[Any] | None = None,
    ) -> None:
        self._run = _MockGraphRun(nodes, result=result, history=history)

    def iter(self, initial_node: Any, *, state: Any = None) -> _MockGraphRun:
        return self._run


@dataclass
class _GraphState:
    value: str = ""
    _easycat_event_handler: Any = None


@dataclass
class _GraphStateNoConvention:
    """State without the convention slot — used for BridgeConfigurationError test."""

    value: str = ""


@dataclass
class _LargeGraphState:
    """State with a large field to trigger 4KB overflow."""

    big_field: str = ""
    _easycat_event_handler: Any = None


# ════════════════════════════════════════════════════════════════════
#  AC2.5 + AC2.17: OpenAI Agents bridge handoff triple
# ════════════════════════════════════════════════════════════════════


class TestOpenAIAgentsBridgeHandoff:
    """AC2.5 — handoff produces FrameworkHandoff record.
    AC2.17 — handoff triple: exit → handoff → enter in sequence.
    """

    @pytest.mark.asyncio
    async def test_handoff_produces_triple_in_journal(self):
        from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge

        agent_a = _MockAgent("AgentA")
        agent_b = _MockAgent("AgentB")

        events = [
            _MockStreamEvent(
                type="raw_response_event",
                data=_MockRawResponseEvent("hello"),
            ),
        ]
        run_result = _MockRunResult(events, last_agent=agent_b)
        mock_runner = _MockRunner(run_result)

        bridge = OpenAIAgentsBridge(agent=agent_a)
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _make_recorder(journal)

        with patch.object(
            type(bridge),
            "invoke",
            wraps=bridge.invoke,
        ):
            # Patch Runner at module level.
            import easycat.integrations.agents.openai_agents as oai_mod

            original_runner = oai_mod.Runner
            oai_mod.Runner = mock_runner
            try:
                async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
                    pass
            finally:
                oai_mod.Runner = original_runner

        records = journal.read()
        names = [r.name for r in records]

        # Must contain the handoff triple.
        assert "unit_exited" in names
        assert "framework_handoff" in names

        # Find the triple: exit(A) → handoff → enter(B).
        # The first exit after the initial enter is the handoff exit.
        exit_indices = [i for i, n in enumerate(names) if n == "unit_exited"]
        handoff_idx = names.index("framework_handoff")
        enter_indices = [i for i, n in enumerate(names) if n == "unit_entered"]

        # The handoff exit is the first exit.
        handoff_exit_idx = exit_indices[0]
        # The enter after the handoff is for AgentB.
        post_handoff_enters = [i for i in enter_indices if i > handoff_idx]
        assert len(post_handoff_enters) >= 1
        handoff_enter_idx = post_handoff_enters[0]

        # Verify ordering: exit < handoff < enter.
        assert handoff_exit_idx < handoff_idx < handoff_enter_idx

        # Verify from_unit/to_unit on the handoff record.
        handoff_record = records[handoff_idx]
        assert handoff_record.data["from_unit"] == "AgentA"
        assert handoff_record.data["to_unit"] == "AgentB"

        # Verify no interleaved records between the triple.
        assert handoff_idx == handoff_exit_idx + 1
        assert handoff_enter_idx == handoff_idx + 1

    @pytest.mark.asyncio
    async def test_no_handoff_when_same_agent(self):
        """When last_agent is the same, no handoff triple is emitted."""
        from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge

        agent_a = _MockAgent("AgentA")

        events = [
            _MockStreamEvent(
                type="raw_response_event",
                data=_MockRawResponseEvent("hi"),
            ),
        ]
        run_result = _MockRunResult(events, last_agent=agent_a)
        mock_runner = _MockRunner(run_result)

        bridge = OpenAIAgentsBridge(agent=agent_a)
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _make_recorder(journal)

        import easycat.integrations.agents.openai_agents as oai_mod

        original_runner = oai_mod.Runner
        oai_mod.Runner = mock_runner
        try:
            async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
                pass
        finally:
            oai_mod.Runner = original_runner

        records = journal.read()
        names = [r.name for r in records]
        assert "framework_handoff" not in names

    @pytest.mark.asyncio
    async def test_handoff_records_tool_calls(self):
        """Tool calls before a handoff are recorded in the journal."""
        from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge

        agent_a = _MockAgent("AgentA")
        agent_b = _MockAgent("AgentB")

        # Simulate: text delta, tool start, tool result, then handoff.
        tool_call_item = type(
            "MockItem",
            (),
            {
                "type": "tool_call_item",
                "raw_item": type("Raw", (), {"name": "get_weather", "call_id": "c1"})(),
            },
        )()
        tool_result_item = type(
            "MockItem",
            (),
            {
                "type": "tool_call_output_item",
                "raw_item": type("Raw", (), {"call_id": "c1"})(),
                "output": "sunny",
            },
        )()

        events = [
            _MockStreamEvent(type="run_item_stream_event", item=tool_call_item),
            _MockStreamEvent(type="run_item_stream_event", item=tool_result_item),
            _MockStreamEvent(
                type="raw_response_event",
                data=_MockRawResponseEvent("The weather is sunny"),
            ),
        ]
        run_result = _MockRunResult(events, last_agent=agent_b)
        mock_runner = _MockRunner(run_result)

        bridge = OpenAIAgentsBridge(agent=agent_a)
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _make_recorder(journal)

        import easycat.integrations.agents.openai_agents as oai_mod

        original_runner = oai_mod.Runner
        oai_mod.Runner = mock_runner
        try:
            async for _ in bridge.invoke(AgentTurnInput.from_text("weather?"), rec):
                pass
        finally:
            oai_mod.Runner = original_runner

        records = journal.read()
        names = [r.name for r in records]

        # Tool phases recorded.
        assert names.count("tool_phase_changed") == 2
        # Handoff triple present.
        assert "framework_handoff" in names


# ════════════════════════════════════════════════════════════════════
#  AC2.6: PydanticAI Agent mode node transitions
# ════════════════════════════════════════════════════════════════════


class TestPydanticAIAgentModeTransitions:
    """AC2.6 — Agent mode: iter() nodes produce paired enter/exit + tool records."""

    @pytest.mark.asyncio
    async def test_tool_call_events_recorded(self):
        """A node with tool call events produces tool_phase_changed records."""
        from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

        nodes = [
            _MockNode(
                "ModelRequestNode",
                stream_events=[
                    PartDeltaEvent(TextPartDelta("Hello ")),
                    PartDeltaEvent(TextPartDelta("world")),
                ],
            ),
            _MockNode(
                "CallToolsNode",
                stream_events=[
                    FunctionToolCallEvent("get_weather"),
                    FunctionToolResultEvent("tc1", "sunny"),
                ],
            ),
        ]
        agent = _MockPydanticAgent("TestAgent", nodes, output="Hello world")
        bridge = PydanticAIBridge(agent=agent)

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _make_recorder(journal)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            events.append(ev)

        records = journal.read()
        names = [r.name for r in records]

        # Agent cursor: enter + exit.
        assert names[0] == "unit_entered"
        assert names[-1] == "unit_exited"

        # Tool phases recorded by translate_event.
        tool_records = [r for r in records if r.name == "tool_phase_changed"]
        assert len(tool_records) >= 2
        phases = [r.data["phase"] for r in tool_records]
        assert "start" in phases
        assert "result" in phases

    @pytest.mark.asyncio
    async def test_text_deltas_yielded(self):
        """Text delta events from iter nodes are yielded as bridge events."""
        from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

        nodes = [
            _MockNode(
                "ModelRequestNode",
                stream_events=[
                    PartDeltaEvent(TextPartDelta("Hello ")),
                    PartDeltaEvent(TextPartDelta("world")),
                ],
            ),
        ]
        agent = _MockPydanticAgent("TestAgent", nodes, output="Hello world")
        bridge = PydanticAIBridge(agent=agent)

        rec = _make_recorder()
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            events.append(ev)

        text_deltas = [e for e in events if e.kind == "text_delta"]
        assert len(text_deltas) == 2
        assert text_deltas[0].text == "Hello "
        assert text_deltas[1].text == "world"

        done_events = [e for e in events if e.kind == "done"]
        assert len(done_events) == 1


# ════════════════════════════════════════════════════════════════════
#  AC2.6a-b: PydanticAI Graph mode transitions + history artifact
# ════════════════════════════════════════════════════════════════════


class TestPydanticAIGraphModeTransitions:
    """AC2.6a — Graph mode transitions produce workflow_node cursors + handoff.
    AC2.6b — Graph mode emits history artifact and active_node matches.
    """

    @pytest.mark.asyncio
    async def test_two_node_graph_produces_handoff_triple(self):
        """Two-node graph produces exit → handoff → enter between nodes."""
        from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

        node_a = _MockGraphNode("ClassifierNode")
        node_b = _MockGraphNode("ResponderNode")

        graph = _MockGraph(
            [node_a, node_b],
            history=[node_a, node_b],
        )

        # The handler must be marked as called to avoid ConventionViolationError.
        state_holder: list[_GraphState] = []

        def state_factory() -> _GraphState:
            s = _GraphState()
            state_holder.append(s)
            return s

        def initial_node_factory(text: str, state: Any) -> _MockGraphNode:
            # Simulate the handler being called by graph node execution.
            if state._easycat_event_handler is not None:
                import asyncio

                asyncio.get_event_loop().run_until_complete(
                    state._easycat_event_handler(PartDeltaEvent(TextPartDelta("response")))
                )
            return node_a

        bridge = PydanticAIBridge(
            graph=graph,
            state_factory=state_factory,
            initial_node_factory=initial_node_factory,
        )

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _make_recorder(journal)

        # We need the handler to be "called" during graph execution.
        # Patch the graph iter to trigger the handler.
        original_invoke_graph = bridge._invoke_graph

        async def patched_invoke_graph(
            turn_input: Any, recorder: Any, cancel_token: Any
        ) -> AsyncIterator:
            async for ev in original_invoke_graph(turn_input, recorder, cancel_token):
                yield ev

        # Instead of complex patching, let's directly mark the handler as called
        # during graph iteration by hooking into the graph run.
        _original_iter = graph.iter

        def _patched_iter(initial_node: Any, *, state: Any = None) -> _MockGraphRun:
            # Mark handler as called.
            handler = getattr(state, "_easycat_event_handler", None)
            if handler is not None:
                handler._was_called = True
            return _original_iter(initial_node, state=state)

        graph.iter = _patched_iter

        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            pass

        records = journal.read()
        names = [r.name for r in records]

        # Two workflow_node enters (ClassifierNode + ResponderNode).
        enters = [r for r in records if r.name == "unit_entered"]
        assert len(enters) == 2
        assert enters[0].data["display_name"] == "ClassifierNode"
        assert enters[1].data["display_name"] == "ResponderNode"

        # Handoff triple between nodes.
        assert "framework_handoff" in names
        handoff_idx = names.index("framework_handoff")
        handoff_data = records[handoff_idx].data
        assert handoff_data["from_unit"] == "ClassifierNode"
        assert handoff_data["to_unit"] == "ResponderNode"

        # Verify ordering: exit(A) → handoff → enter(B).
        exit_indices = [i for i, n in enumerate(names) if n == "unit_exited"]
        enter_indices = [i for i, n in enumerate(names) if n == "unit_entered"]

        first_exit = exit_indices[0]
        second_enter = enter_indices[1]
        assert first_exit < handoff_idx < second_enter

        # Verify no interleaved records in the triple.
        assert handoff_idx == first_exit + 1
        assert second_enter == handoff_idx + 1

    @pytest.mark.asyncio
    async def test_graph_history_snapshot_recorded(self):
        """AC2.6b — graph run history produces a state_snapshot record."""
        from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

        node_a = _MockGraphNode("NodeA")
        history_nodes = [_MockGraphNode("NodeA")]

        graph = _MockGraph([node_a], history=history_nodes)

        def state_factory() -> _GraphState:
            return _GraphState()

        def initial_node_factory(text: str, state: Any) -> _MockGraphNode:
            return node_a

        bridge = PydanticAIBridge(
            graph=graph,
            state_factory=state_factory,
            initial_node_factory=initial_node_factory,
        )

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _make_recorder(journal)

        # Patch graph.iter to mark handler as called.
        _original_iter = graph.iter

        def _patched_iter(initial_node: Any, *, state: Any = None) -> _MockGraphRun:
            handler = getattr(state, "_easycat_event_handler", None)
            if handler is not None:
                handler._was_called = True
            return _original_iter(initial_node, state=state)

        graph.iter = _patched_iter

        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            pass

        records = journal.read()
        snapshot_records = [r for r in records if r.name == "state_snapshot"]
        assert len(snapshot_records) >= 1
        assert snapshot_records[0].data["state_ref"].startswith("graph-history-")

    @pytest.mark.asyncio
    async def test_active_node_matches_last_node(self):
        """AC2.6b — active_node in snapshot matches the last graph node."""
        from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

        node_a = _MockGraphNode("FirstNode")
        node_b = _MockGraphNode("LastNode")

        graph = _MockGraph([node_a, node_b], history=[node_a, node_b])

        def state_factory() -> _GraphState:
            return _GraphState()

        def initial_node_factory(text: str, state: Any) -> _MockGraphNode:
            return node_a

        bridge = PydanticAIBridge(
            graph=graph,
            state_factory=state_factory,
            initial_node_factory=initial_node_factory,
        )

        # Patch to avoid convention error.
        _original_iter = graph.iter

        def _patched_iter(initial_node: Any, *, state: Any = None) -> _MockGraphRun:
            handler = getattr(state, "_easycat_event_handler", None)
            if handler is not None:
                handler._was_called = True
            return _original_iter(initial_node, state=state)

        graph.iter = _patched_iter

        rec = _make_recorder()
        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            pass

        snap = bridge.snapshot_state()
        assert snap.fields["active_node"] == "LastNode"


# ════════════════════════════════════════════════════════════════════
#  AC2.6c: PydanticAI snapshot 4KB overflow via bridge
# ════════════════════════════════════════════════════════════════════


class TestPydanticAISnapshotOverflow:
    """AC2.6c — large state produces artifact ref, inline < 4KB."""

    def test_graph_mode_large_state_uses_artifact_ref(self):
        """A graph state > 4KB triggers state_ref in snapshot."""
        from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

        node = _MockGraphNode("Node")
        graph = _MockGraph([node])

        def state_factory() -> _LargeGraphState:
            return _LargeGraphState()

        def initial_node_factory(text: str, state: Any) -> _MockGraphNode:
            return node

        bridge = PydanticAIBridge(
            graph=graph,
            state_factory=state_factory,
            initial_node_factory=initial_node_factory,
        )

        # Simulate a large state (500KB) being set during a turn.
        bridge._state = _LargeGraphState(big_field="x" * 500_000)

        snap = bridge.snapshot_state()
        assert snap.state_ref is not None
        assert snap.state_ref.startswith("state-")
        # Inline fields must be < 4KB.
        inline_size = len(json.dumps(snap.fields))
        assert inline_size < 4096

    def test_graph_mode_small_state_inlined(self):
        """A graph state < 4KB is inlined without state_ref."""
        from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

        node = _MockGraphNode("Node")
        graph = _MockGraph([node])

        def state_factory() -> _LargeGraphState:
            return _LargeGraphState()

        def initial_node_factory(text: str, state: Any) -> _MockGraphNode:
            return node

        bridge = PydanticAIBridge(
            graph=graph,
            state_factory=state_factory,
            initial_node_factory=initial_node_factory,
        )

        bridge._state = _LargeGraphState(big_field="small")
        snap = bridge.snapshot_state()
        assert snap.state_ref is None
        assert "state" in snap.fields  # inlined, not state_summary

    def test_agent_mode_snapshot_always_small(self):
        """Agent mode snapshot is always small — no state_ref."""
        from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

        agent = _MockPydanticAgent("TestAgent", [])
        bridge = PydanticAIBridge(agent=agent)

        snap = bridge.snapshot_state()
        assert snap.state_ref is None
        assert snap.kind == "pydantic_ai_agent"
        inline_size = len(json.dumps(snap.fields))
        assert inline_size < 4096


# ════════════════════════════════════════════════════════════════════
#  AC2.6d: Runtime ConventionViolationError
# ════════════════════════════════════════════════════════════════════


class TestPydanticAIGraphConventionEnforcement:
    """AC2.6d — runtime ConventionViolationError when handler is not used."""

    @pytest.mark.asyncio
    async def test_handler_not_called_raises_convention_error(self):
        """Graph mode raises ConventionViolationError if handler not invoked."""
        from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

        node = _MockGraphNode("SomeNode")
        graph = _MockGraph([node])

        def state_factory() -> _GraphState:
            return _GraphState()

        def initial_node_factory(text: str, state: Any) -> _MockGraphNode:
            return node

        bridge = PydanticAIBridge(
            graph=graph,
            state_factory=state_factory,
            initial_node_factory=initial_node_factory,
        )

        rec = _make_recorder()

        with pytest.raises(ConventionViolationError, match="_easycat_event_handler"):
            async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
                pass

    @pytest.mark.asyncio
    async def test_handler_called_no_error(self):
        """Graph mode succeeds when the handler is invoked by a node."""
        from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

        node = _MockGraphNode("SomeNode")
        graph = _MockGraph([node])

        def state_factory() -> _GraphState:
            return _GraphState()

        def initial_node_factory(text: str, state: Any) -> _MockGraphNode:
            return node

        bridge = PydanticAIBridge(
            graph=graph,
            state_factory=state_factory,
            initial_node_factory=initial_node_factory,
        )

        # Patch graph.iter to mark handler as called.
        _original_iter = graph.iter

        def _patched_iter(initial_node: Any, *, state: Any = None) -> _MockGraphRun:
            handler = getattr(state, "_easycat_event_handler", None)
            if handler is not None:
                handler._was_called = True
            return _original_iter(initial_node, state=state)

        graph.iter = _patched_iter

        rec = _make_recorder()

        # Should not raise.
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            events.append(ev)
        done = [e for e in events if e.kind == "done"]
        assert len(done) == 1


# ════════════════════════════════════════════════════════════════════
#  AC2.8: Committable flag state machine
# ════════════════════════════════════════════════════════════════════


class TestCommittableFlagStateMachine:
    """AC2.8 — cursor starts committable=False, ends committable=True."""

    @pytest.mark.asyncio
    async def test_pydantic_ai_agent_committable_lifecycle(self):
        """Agent mode: entered with committable=False, exited with committable=True."""
        from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

        nodes = [
            _MockNode(
                "ModelRequestNode",
                stream_events=[PartDeltaEvent(TextPartDelta("hi"))],
            ),
        ]
        agent = _MockPydanticAgent("TestAgent", nodes, output="hi")
        bridge = PydanticAIBridge(agent=agent)

        journal = InMemoryRingBuffer(capacity=1000)
        rec = _make_recorder(journal)

        async for _ in bridge.invoke(AgentTurnInput.from_text("hello"), rec):
            pass

        records = journal.read()
        enters = [r for r in records if r.name == "unit_entered"]
        exits = [r for r in records if r.name == "unit_exited"]

        assert len(enters) >= 1
        assert len(exits) >= 1

        # First enter: committable=False (streaming not yet done).
        assert enters[0].data["committable"] is False
        # Last exit: committable=True (turn complete).
        assert exits[-1].data["committable"] is True

    @pytest.mark.asyncio
    async def test_openai_agents_committable_lifecycle(self):
        """OpenAI bridge: entered with committable=False, exited with committable=True."""
        from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge

        agent = _MockAgent("TestAgent")
        events = [
            _MockStreamEvent(
                type="raw_response_event",
                data=_MockRawResponseEvent("hi"),
            ),
        ]
        run_result = _MockRunResult(events, last_agent=agent)
        mock_runner = _MockRunner(run_result)

        bridge = OpenAIAgentsBridge(agent=agent)
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _make_recorder(journal)

        import easycat.integrations.agents.openai_agents as oai_mod

        original_runner = oai_mod.Runner
        oai_mod.Runner = mock_runner
        try:
            async for _ in bridge.invoke(AgentTurnInput.from_text("hello"), rec):
                pass
        finally:
            oai_mod.Runner = original_runner

        records = journal.read()
        enters = [r for r in records if r.name == "unit_entered"]
        exits = [r for r in records if r.name == "unit_exited"]

        assert len(enters) >= 1
        assert len(exits) >= 1
        assert enters[0].data["committable"] is False
        assert exits[-1].data["committable"] is True

    @pytest.mark.asyncio
    async def test_generic_workflow_committable_lifecycle(self):
        """GenericWorkflowBridge: entered committable=False, exited committable=True."""
        from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge

        class _W:
            async def on_user_turn(self, text: str) -> str:
                return text

        bridge = GenericWorkflowBridge(workflow=_W())
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _make_recorder(journal)

        async for _ in bridge.invoke(AgentTurnInput.from_text("hello"), rec):
            pass

        records = journal.read()
        enters = [r for r in records if r.name == "unit_entered"]
        exits = [r for r in records if r.name == "unit_exited"]

        assert len(enters) >= 1
        assert len(exits) >= 1
        assert enters[0].data["committable"] is False
        assert exits[-1].data["committable"] is True
