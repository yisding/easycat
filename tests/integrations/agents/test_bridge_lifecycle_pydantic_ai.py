"""WS3.1 lifecycle driver for the PydanticAI bridge."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Self

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentTurnInput,
    CancellationMode,
    FrameworkStateSnapshot,
)
from easycat.integrations.agents.pydantic_ai import PydanticAIBridge
from easycat.testing import RecordingAgentRecorder
from easycat.testing._bridge_lifecycle import (
    BridgeLifecycleScenarioSuite,
    HistoryIsolationObservation,
    NormalizedHistoryEntry,
    NormalizedLifecycleState,
    RecorderCleanupObservation,
    StreamCloseObservation,
    ToolCancellationObservation,
    UnknownEventObservation,
)

pytestmark = [
    pytest.mark.agent_bridge,
    pytest.mark.surface_agent,
    pytest.mark.provider("pydantic-ai-lifecycle-driver"),
]


class TextPart:
    def __init__(self, content: str) -> None:
        self.content = content


class UserPromptPart:
    def __init__(self, content: str) -> None:
        self.content = content


class SystemPromptPart:
    def __init__(self, content: str) -> None:
        self.content = content


class ModelRequest:
    def __init__(self, *, parts: list[Any]) -> None:
        self.parts = parts


class ModelResponse:
    def __init__(self, *, parts: list[Any]) -> None:
        self.parts = parts


@pytest.fixture(autouse=True)
def _install_message_types(monkeypatch: pytest.MonkeyPatch) -> None:
    package = ModuleType("pydantic_ai")
    messages = ModuleType("pydantic_ai.messages")
    messages.ModelRequest = ModelRequest
    messages.ModelResponse = ModelResponse
    messages.SystemPromptPart = SystemPromptPart
    messages.TextPart = TextPart
    messages.UserPromptPart = UserPromptPart
    package.messages = messages
    monkeypatch.setitem(sys.modules, "pydantic_ai", package)
    monkeypatch.setitem(sys.modules, "pydantic_ai.messages", messages)


class TextPartDelta:
    def __init__(self, text: str) -> None:
        self.content_delta = text


class PartDeltaEvent:
    def __init__(self, text: str) -> None:
        self.delta = TextPartDelta(text)
        self.index = 0


class _ToolCallPart:
    tool_name = "lookup"
    tool_call_id = "call-1"


class _ToolReturnPart:
    tool_name = "lookup"
    tool_call_id = "call-1"
    content = "ok"


class FunctionToolCallEvent:
    part = _ToolCallPart()


class FunctionToolResultEvent:
    part = _ToolReturnPart()
    result = "ok"


class FuturePydanticEvent:
    pass


class _ControlledNodeStream:
    def __init__(self, items: list[object]) -> None:
        self._items = items
        self._index = 0
        self.started = False
        self.exhausted = False
        self.closed = False
        self.waiting = asyncio.Event()
        self.close_calls = 0
        self.running_work_cancelled = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    def __aiter__(self) -> _ControlledNodeStream:
        return self

    async def __anext__(self) -> Any:
        self.started = True
        while not self.closed and self._index < len(self._items):
            item = self._items[self._index]
            self._index += 1
            if isinstance(item, asyncio.Event):
                self.waiting.set()
                await item.wait()
                continue
            return item
        if not self.closed:
            self.exhausted = True
        raise StopAsyncIteration

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_calls += 1
        self.running_work_cancelled = not self.exhausted


class ModelRequestNode:
    def __init__(self, stream: _ControlledNodeStream) -> None:
        self._stream = stream

    def stream(self, _ctx: object) -> _ControlledNodeStream:
        return self._stream


class CallToolsNode:
    def __init__(self, stream: _ControlledNodeStream) -> None:
        self._stream = stream

    def stream(self, _ctx: object) -> _ControlledNodeStream:
        return self._stream


class _ControlledAgentRun:
    def __init__(self, nodes: list[object], new_messages: list[Any]) -> None:
        self._nodes = nodes
        self._index = 0
        self._new_messages = new_messages
        self.ctx = object()
        self.output = None
        self.result = None
        self.active = False

    async def __aenter__(self) -> Self:
        self.active = True
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.active = False

    def __aiter__(self) -> _ControlledAgentRun:
        return self

    async def __anext__(self) -> object:
        if self._index >= len(self._nodes):
            raise StopAsyncIteration
        node = self._nodes[self._index]
        self._index += 1
        return node

    def new_messages(self) -> list[Any]:
        return list(self._new_messages)


class _ControlledAgent:
    name = "LifecycleAgent"
    model = "pydantic-lifecycle"

    def __init__(self) -> None:
        self.run: _ControlledAgentRun | None = None

    def iter(
        self,
        text: str,
        *,
        message_history: list[Any] | None = None,
        deps: Any = None,
        model_settings: Any = None,
    ) -> _ControlledAgentRun:
        del text, message_history, deps, model_settings
        assert self.run is not None
        return self.run


def _user(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(text)])


def _assistant(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(text)])


def _recorder_cursor_ids(
    recorder: RecordingAgentRecorder,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    entered = tuple(
        record[1][0].unit_id for record in recorder.records if record[0] == "unit_entered"
    )
    exited = tuple(
        record[1][0].unit_id for record in recorder.records if record[0] == "unit_exited"
    )
    return entered, exited


class _PydanticAILifecycleDriver:
    def __init__(self) -> None:
        self.agent = _ControlledAgent()
        self.bridge = PydanticAIBridge(agent=self.agent)
        self.run: _ControlledAgentRun | None = None
        self.source: _ControlledNodeStream | None = None

    def _configure(
        self,
        nodes: list[object],
        *,
        new_messages: list[Any],
        observed_source: _ControlledNodeStream,
    ) -> _ControlledNodeStream:
        self.run = _ControlledAgentRun(nodes, new_messages)
        self.agent.run = self.run
        self.source = observed_source
        return observed_source

    async def observe_unknown_event_tolerance(self, *, valid_text: str) -> UnknownEventObservation:
        source = _ControlledNodeStream([FuturePydanticEvent(), PartDeltaEvent(valid_text)])
        self._configure(
            [ModelRequestNode(source)],
            new_messages=[_user("unknown event"), _assistant(valid_text)],
            observed_source=source,
        )
        events = tuple(
            [
                event
                async for event in self.bridge.invoke(
                    AgentTurnInput.from_text("unknown event"), RecordingAgentRecorder()
                )
            ]
        )
        return UnknownEventObservation(events=events)

    async def observe_tool_inflight_cancellation(
        self, *, delivered_text: str
    ) -> ToolCancellationObservation:
        model_source = _ControlledNodeStream([PartDeltaEvent(delivered_text)])
        gate = asyncio.Event()
        tool_source = _ControlledNodeStream(
            [
                FunctionToolCallEvent(),
                gate,
                FunctionToolResultEvent(),
                PartDeltaEvent("must not be delivered"),
            ]
        )
        self._configure(
            [ModelRequestNode(model_source), CallToolsNode(tool_source)],
            new_messages=[_user("use a tool"), _assistant(delivered_text)],
            observed_source=tool_source,
        )
        token = CancelToken()
        recorder = RecordingAgentRecorder()
        stream = self.bridge.invoke(AgentTurnInput.from_text("use a tool"), recorder, token)
        before_cancel: list[AgentBridgeEvent] = []
        while True:
            event = await stream.__anext__()
            before_cancel.append(event)
            if event.kind == "tool_started":
                break
        phases_before_cancel = tuple(recorder.tool_phases())
        pending_result = asyncio.create_task(stream.__anext__())
        await tool_source.waiting.wait()
        token.cancel()
        gate.set()
        after_cancel = [await pending_result]
        after_cancel.extend([event async for event in stream])
        phases = tuple(recorder.tool_phases())
        committed_assistant_text = self._history()[-1].text
        self.bridge.apply_interruption(
            delivered_text,
            CancellationMode.DRAIN_CURRENT_UNIT,
            recorder=recorder,
        )

        return ToolCancellationObservation(
            events_before_cancel=tuple(before_cancel),
            events_after_cancel=tuple(after_cancel),
            tool_phases_before_cancel=phases_before_cancel,
            tool_phases_after_cancel=phases[len(phases_before_cancel) :],
            committed_assistant_text=committed_assistant_text,
            inner_stream_close_calls=tool_source.close_calls,
        )

    async def observe_stream_close_cleanup(self) -> StreamCloseObservation:
        gate = asyncio.Event()
        source = _ControlledNodeStream(
            [PartDeltaEvent("partial"), gate, PartDeltaEvent("must not be delivered")]
        )
        self._configure(
            [ModelRequestNode(source)],
            new_messages=[_user("close"), _assistant("partial")],
            observed_source=source,
        )
        stream = self.bridge.invoke(AgentTurnInput.from_text("close"), RecordingAgentRecorder())
        await stream.__anext__()
        await stream.aclose()
        return StreamCloseObservation(
            inner_stream_close_calls=source.close_calls,
            running_work_cancelled=source.running_work_cancelled,
        )

    async def observe_recorder_transient_cleanup(self) -> RecorderCleanupObservation:
        gate = asyncio.Event()
        source = _ControlledNodeStream([FunctionToolCallEvent(), gate, FunctionToolResultEvent()])
        self._configure(
            [CallToolsNode(source)],
            new_messages=[_user("cleanup")],
            observed_source=source,
        )
        recorder = RecordingAgentRecorder()
        stream = self.bridge.invoke(AgentTurnInput.from_text("cleanup"), recorder)
        await stream.__anext__()
        await stream.aclose()
        entered, exited = _recorder_cursor_ids(recorder)
        return RecorderCleanupObservation(
            entered_cursor_ids=entered,
            exited_cursor_ids=exited,
            transient_items_after_close=self._transient_items(),
            inner_stream_close_calls=source.close_calls,
        )

    async def observe_interruption_history_isolation(
        self, *, prior_user_text: str, prior_assistant_text: str
    ) -> HistoryIsolationObservation:
        recorder = RecordingAgentRecorder()
        history_key = self.bridge._history_key_for_recorder(recorder)
        prior = [_user(prior_user_text), _assistant(prior_assistant_text)]
        self.bridge._set_history_for_key(history_key, prior)
        before = self._project_prior_history()
        gate = asyncio.Event()
        source = _ControlledNodeStream([gate, PartDeltaEvent("must not be delivered")])
        self._configure(
            [ModelRequestNode(source)],
            new_messages=[_user("new turn")],
            observed_source=source,
        )
        stream = self.bridge.invoke(AgentTurnInput.from_text("new turn"), recorder)
        pending_event = asyncio.create_task(stream.__anext__())
        await source.waiting.wait()
        pending_event.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending_event
        await stream.aclose()
        self.bridge.apply_interruption(
            "",
            CancellationMode.IMMEDIATE_STOP,
            recorder=recorder,
        )
        return HistoryIsolationObservation(
            prior_history_before=before,
            prior_history_after=self._project_prior_history(),
        )

    def _history(self) -> tuple[NormalizedHistoryEntry, ...]:
        normalized: list[NormalizedHistoryEntry] = []
        for message in self.bridge._history_for_key(self.bridge._last_history_key):
            if isinstance(message, ModelRequest):
                text = "".join(
                    part.content for part in message.parts if isinstance(part, UserPromptPart)
                )
                if text:
                    normalized.append(NormalizedHistoryEntry(role="user", text=text))
            elif isinstance(message, ModelResponse):
                text = "".join(
                    part.content for part in message.parts if isinstance(part, TextPart)
                )
                normalized.append(NormalizedHistoryEntry(role="assistant", text=text))
        return tuple(normalized)

    def _project_prior_history(self) -> tuple[NormalizedHistoryEntry, ...]:
        return self._history()[:2]

    def _transient_items(self) -> int:
        active_source = int(bool(self.source and self.source.started and not self.source.closed))
        active_run = int(bool(self.run and self.run.active))
        return active_source + active_run

    def normalized_state(self) -> NormalizedLifecycleState:
        return NormalizedLifecycleState(
            history=self._history(),
            active_streams=int(bool(self.run and self.run.active)),
            transient_items=self._transient_items(),
        )

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return self.bridge.snapshot_state()

    def reset(self) -> None:
        self.bridge.reset()
        self.agent.run = None
        self.run = None
        self.source = None


class TestPydanticAIBridgeLifecycleScenarios(BridgeLifecycleScenarioSuite):
    driver_factory = _PydanticAILifecycleDriver


def test_pydantic_ai_suite_scenarios_match_execution_matrix() -> None:
    matrix_path = Path(__file__).with_name("bridge-lifecycle-matrix.json")
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    execution = matrix["driver_execution"]["bridges"]["pydantic_ai"]

    assert execution["status"] == "wired"
    assert set(execution["scenarios"]) == (
        TestPydanticAIBridgeLifecycleScenarios.applicable_scenarios
    )
