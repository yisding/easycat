"""WS3.1 lifecycle driver for the Llama Agents bridge."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents.base import (
    AgentTurnInput,
    CancellationMode,
    FrameworkStateSnapshot,
)
from easycat.integrations.agents.llama_agents import LlamaAgentsBridge
from easycat.testing import RecordingAgentRecorder
from easycat.testing._bridge_lifecycle import (
    BridgeLifecycleScenario,
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
    pytest.mark.provider("llama-agents-lifecycle-driver"),
]

LLAMA_AGENTS_SCENARIOS: frozenset[BridgeLifecycleScenario] = frozenset(
    {
        "recorder_transient_cleanup",
        "stream_close_cleanup",
        "tool_inflight_cancellation_drain",
        "unknown_event_tolerance",
    }
)


@pytest.fixture(autouse=True)
def _install_workflow_types(fake_workflows_modules: None) -> None:
    pass


class _TextEvent:
    def __init__(self, text: str) -> None:
        self.delta = text


class _FutureWorkflowEvent:
    pass


class _ToolStartBoundary:
    pass


class _Context:
    def to_dict(self) -> dict[str, Any]:
        return {"state": {"lifecycle": "controlled"}}


class _ControlledWorkflowStream:
    def __init__(self, items: list[object]) -> None:
        self._items = items
        self._index = 0
        self._on_item: Callable[[object], None] | None = None
        self.started = False
        self.exhausted = False
        self.closed = False
        self.waiting = asyncio.Event()
        self.close_calls = 0
        self.running_work_cancelled = False

    def __aiter__(self) -> _ControlledWorkflowStream:
        return self

    async def __anext__(self) -> object:
        self.started = True
        while not self.closed and self._index < len(self._items):
            item = self._items[self._index]
            self._index += 1
            if isinstance(item, asyncio.Event):
                self.waiting.set()
                await item.wait()
                continue
            if self._on_item is not None:
                self._on_item(item)
            return item
        if not self.closed:
            self.exhausted = True
            self.closed = True
        raise StopAsyncIteration

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_calls += 1
        self.running_work_cancelled = not self.exhausted


class _ControlledHandler:
    def __init__(
        self,
        source: _ControlledWorkflowStream,
        *,
        result: str,
        recorder: RecordingAgentRecorder | None,
        terminal_on_cancel: bool,
    ) -> None:
        self.source = source
        self.result = result
        self.recorder = recorder
        self.terminal_on_cancel = terminal_on_cancel
        self.ctx = _Context()
        self.run_id = "lifecycle-run"
        self.cancelled = False
        self.done = False
        self.tool_pending = False
        self.tool_started = asyncio.Event()
        source._on_item = self._observe_item

    def _observe_item(self, item: object) -> None:
        if not isinstance(item, _ToolStartBoundary):
            return
        self.tool_pending = True
        if self.recorder is not None:
            self.recorder.record_tool_call(
                phase="start",
                name="lookup",
                call_id="call-1",
            )
        self.tool_started.set()

    def __await__(self):
        async def _result() -> str:
            self.done = True
            return self.result

        return _result().__await__()

    def stream_events(self) -> _ControlledWorkflowStream:
        return self.source

    async def cancel_run(self) -> None:
        self.cancelled = True
        if self.tool_pending:
            if self.recorder is not None:
                self.recorder.record_tool_call(
                    phase="result",
                    name="lookup",
                    call_id="call-1",
                )
            self.tool_pending = False
        self.done = self.terminal_on_cancel

    def is_done(self) -> bool:
        return self.done


class _ControlledWorkflow:
    def __init__(self) -> None:
        self.handler: _ControlledHandler | None = None
        self.history: list[NormalizedHistoryEntry] = []
        self.interruption_notes: list[str] = []

    def run(self, **kwargs: Any) -> _ControlledHandler:
        del kwargs
        assert self.handler is not None
        return self.handler

    def replace_last_assistant_text(self, text: str) -> None:
        for index in range(len(self.history) - 1, -1, -1):
            if self.history[index].role == "assistant":
                self.history[index] = NormalizedHistoryEntry(role="assistant", text=text)
                return

    def append_interruption_note(self, note: str) -> None:
        self.interruption_notes.append(note)

    def reset(self) -> None:
        self.handler = None
        self.history.clear()
        self.interruption_notes.clear()


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


class _LlamaAgentsLifecycleDriver:
    def __init__(self) -> None:
        self.workflow = _ControlledWorkflow()
        self.bridge = LlamaAgentsBridge(workflow=self.workflow)
        self.source: _ControlledWorkflowStream | None = None
        self.handler: _ControlledHandler | None = None

    def _configure(
        self,
        items: list[object],
        *,
        recorder: RecordingAgentRecorder | None = None,
        result: str = "",
        terminal_on_cancel: bool = True,
    ) -> _ControlledHandler:
        source = _ControlledWorkflowStream(items)
        handler = _ControlledHandler(
            source,
            result=result,
            recorder=recorder,
            terminal_on_cancel=terminal_on_cancel,
        )
        self.source = source
        self.handler = handler
        self.workflow.handler = handler
        return handler

    async def observe_unknown_event_tolerance(self, *, valid_text: str) -> UnknownEventObservation:
        self._configure(
            [_FutureWorkflowEvent(), _TextEvent(valid_text)],
            result=valid_text,
        )
        events = tuple(
            [
                event
                async for event in self.bridge.invoke(
                    AgentTurnInput.from_text("unknown event"), RecordingAgentRecorder()
                )
            ]
        )
        self.workflow.history = [
            NormalizedHistoryEntry(role="user", text="unknown event"),
            NormalizedHistoryEntry(role="assistant", text=valid_text),
        ]
        return UnknownEventObservation(events=events)

    async def observe_tool_inflight_cancellation(
        self, *, delivered_text: str
    ) -> ToolCancellationObservation:
        gate = asyncio.Event()
        token = CancelToken()
        recorder = RecordingAgentRecorder()
        handler = self._configure(
            [_TextEvent(delivered_text), _ToolStartBoundary(), gate],
            recorder=recorder,
            result=delivered_text,
        )
        stream = self.bridge.invoke(AgentTurnInput.from_text("use a tool"), recorder, token)
        before_cancel = (await stream.__anext__(),)
        pending_terminal = asyncio.create_task(stream.__anext__())
        await handler.tool_started.wait()
        phases_before_cancel = tuple(recorder.tool_phases())
        token.cancel()
        after_cancel = [await pending_terminal]
        after_cancel.extend([event async for event in stream])
        phases = tuple(recorder.tool_phases())
        self.workflow.history = [
            NormalizedHistoryEntry(role="user", text="use a tool"),
            NormalizedHistoryEntry(role="assistant", text=delivered_text),
        ]
        committed_assistant_text = self.workflow.history[-1].text
        self.bridge.apply_interruption(
            delivered_text,
            CancellationMode.DRAIN_CURRENT_UNIT,
            recorder=recorder,
        )
        assert self.source is not None

        return ToolCancellationObservation(
            events_before_cancel=before_cancel,
            events_after_cancel=tuple(after_cancel),
            tool_phases_before_cancel=phases_before_cancel,
            tool_phases_after_cancel=phases[len(phases_before_cancel) :],
            committed_assistant_text=committed_assistant_text,
            inner_stream_close_calls=self.source.close_calls,
        )

    async def observe_stream_close_cleanup(self) -> StreamCloseObservation:
        gate = asyncio.Event()
        handler = self._configure(
            [_TextEvent("partial"), gate, _TextEvent("must not be delivered")],
            result="partial",
        )
        stream = self.bridge.invoke(AgentTurnInput.from_text("close"), RecordingAgentRecorder())
        await stream.__anext__()
        await stream.aclose()
        assert self.source is not None
        assert handler.cancelled
        return StreamCloseObservation(
            inner_stream_close_calls=self.source.close_calls,
            running_work_cancelled=self.source.running_work_cancelled,
        )

    async def observe_recorder_transient_cleanup(self) -> RecorderCleanupObservation:
        gate = asyncio.Event()
        recorder = RecordingAgentRecorder()
        handler = self._configure(
            [_ToolStartBoundary(), _TextEvent("partial"), gate],
            recorder=recorder,
            result="partial",
            terminal_on_cancel=False,
        )
        stream = self.bridge.invoke(AgentTurnInput.from_text("cleanup"), recorder)
        await stream.__anext__()
        assert handler.tool_pending
        await stream.aclose()
        entered, exited = _recorder_cursor_ids(recorder)
        assert self.source is not None
        return RecorderCleanupObservation(
            entered_cursor_ids=entered,
            exited_cursor_ids=exited,
            transient_items_after_close=self._transient_items(),
            inner_stream_close_calls=self.source.close_calls,
        )

    async def observe_interruption_history_isolation(
        self, *, prior_user_text: str, prior_assistant_text: str
    ) -> HistoryIsolationObservation:
        del prior_user_text, prior_assistant_text
        raise AssertionError("Llama Agents carries interruption metadata, not assistant history")

    def _transient_items(self) -> int:
        values = (
            self.bridge._active_handler,
            self.bridge._active_handler_id,
            self.bridge._pending_local_handler,
            self.bridge._pending_local_stream,
            self.bridge._pending_interruption_note,
            self.bridge._ctx,
        )
        active_source = bool(self.source and self.source.started and not self.source.closed)
        return sum(value is not None for value in values) + int(active_source)

    def normalized_state(self) -> NormalizedLifecycleState:
        return NormalizedLifecycleState(
            history=tuple(self.workflow.history),
            active_streams=int(bool(self.bridge._active_handler)),
            transient_items=self._transient_items(),
        )

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return self.bridge.snapshot_state()

    def reset(self) -> None:
        self.bridge.reset()
        self.source = None
        self.handler = None


class TestLlamaAgentsBridgeLifecycleScenarios(BridgeLifecycleScenarioSuite):
    driver_factory = _LlamaAgentsLifecycleDriver
    applicable_scenarios = LLAMA_AGENTS_SCENARIOS


def test_llama_agents_suite_scenarios_match_execution_matrix() -> None:
    matrix_path = Path(__file__).with_name("bridge-lifecycle-matrix.json")
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    execution = matrix["driver_execution"]["bridges"]["llama_agents"]

    assert execution["status"] == "wired"
    assert set(execution["scenarios"]) == (
        TestLlamaAgentsBridgeLifecycleScenarios.applicable_scenarios
    )
