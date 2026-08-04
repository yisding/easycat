"""WS3.1 lifecycle drivers for bridges that require no optional SDK extras."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    CancellationMode,
    ExecutionCursor,
    FrameworkStateSnapshot,
    UnitKind,
)
from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge
from easycat.integrations.agents.responses_api import RemoteResponsesAPIBridge
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
    pytest.mark.provider("credential-free-lifecycle-driver"),
]

GENERIC_WORKFLOW_SCENARIOS: frozenset[BridgeLifecycleScenario] = frozenset(
    {
        "interruption_prior_turn_isolation",
        "recorder_transient_cleanup",
        "stream_close_cleanup",
        "tool_inflight_cancellation_drain",
    }
)


class _GenericLifecycleWorkflow:
    """Deep workflow with gates at every lifecycle boundary the bridge exposes."""

    def __init__(self) -> None:
        self.mode = ""
        self.gate = asyncio.Event()
        self.waiting = asyncio.Event()
        self.history: list[NormalizedHistoryEntry] = []
        self.transient_items: set[str] = set()
        self.active_streams = 0
        self.close_calls = 0
        self.running_work_cancelled = False
        self.current_user_text = ""
        self.delivered_text = ""

    def configure(self, mode: str, *, delivered_text: str = "") -> None:
        assert self.active_streams == 0
        self.mode = mode
        self.gate = asyncio.Event()
        self.waiting = asyncio.Event()
        self.close_calls = 0
        self.running_work_cancelled = False
        self.current_user_text = ""
        self.delivered_text = delivered_text

    async def on_user_turn(
        self,
        text: str,
        *,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[str]:
        del cancel_token
        self.current_user_text = text
        self.active_streams += 1
        completed = False
        nested_cursor: ExecutionCursor | None = None
        try:
            if self.mode == "tool":
                yield self.delivered_text
                recorder.record_tool_call("start", "lookup", call_id="call-1")
                self.waiting.set()
                await self.gate.wait()
                recorder.record_tool_call(
                    "result",
                    "lookup",
                    result_ref="result-ref",
                    call_id="call-1",
                )
                yield "must not be delivered"
            elif self.mode == "stream_close":
                yield "partial"
                self.waiting.set()
                await self.gate.wait()
                yield "must not be delivered"
            elif self.mode == "recorder_cleanup":
                nested_cursor = ExecutionCursor(
                    unit_id="generic-nested",
                    unit_kind=UnitKind.TOOL_CALL,
                )
                recorder.record_unit_entered(nested_cursor)
                self.transient_items.add(nested_cursor.unit_id)
                yield "partial"
                self.waiting.set()
                await self.gate.wait()
                yield "must not be delivered"
            elif self.mode == "history_isolation":
                self.waiting.set()
                await self.gate.wait()
                yield "must not be delivered"
            completed = True
        finally:
            if nested_cursor is not None:
                recorder.safe_exit_cursor(nested_cursor)
                self.transient_items.discard(nested_cursor.unit_id)
            self.running_work_cancelled = not completed
            self.active_streams -= 1
            self.close_calls += 1

    def apply_interruption(self, delivered_text: str, mode: CancellationMode) -> None:
        del mode
        if not delivered_text or not self.current_user_text:
            return
        self.history.extend(
            (
                NormalizedHistoryEntry(role="user", text=self.current_user_text),
                NormalizedHistoryEntry(role="assistant", text=delivered_text),
            )
        )

    def seed_prior_turn(self, user_text: str, assistant_text: str) -> None:
        self.history = [
            NormalizedHistoryEntry(role="user", text=user_text),
            NormalizedHistoryEntry(role="assistant", text=assistant_text),
        ]

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "history": [{"role": entry.role, "text": entry.text} for entry in self.history],
            "active_streams": self.active_streams,
            "transient_items": sorted(self.transient_items),
        }

    def reset(self) -> None:
        assert self.active_streams == 0
        self.history.clear()
        self.transient_items.clear()
        self.current_user_text = ""
        self.delivered_text = ""
        self.close_calls = 0
        self.running_work_cancelled = False


class _GenericWorkflowLifecycleDriver:
    def __init__(self) -> None:
        self.workflow = _GenericLifecycleWorkflow()
        self.bridge = GenericWorkflowBridge(self.workflow)

    async def observe_unknown_event_tolerance(self, *, valid_text: str) -> UnknownEventObservation:
        del valid_text
        raise AssertionError("generic workflow has no provider event taxonomy")

    async def observe_tool_inflight_cancellation(
        self, *, delivered_text: str
    ) -> ToolCancellationObservation:
        self.workflow.configure("tool", delivered_text=delivered_text)
        token = CancelToken()
        recorder = RecordingAgentRecorder()
        stream = self.bridge.invoke(AgentTurnInput.from_text("use a tool"), recorder, token)
        before_cancel = (await stream.__anext__(),)
        pending_terminal = asyncio.create_task(stream.__anext__())
        await self.workflow.waiting.wait()
        phases_before_cancel = tuple(recorder.tool_phases())
        token.cancel()
        self.workflow.gate.set()
        after_cancel = [await pending_terminal]
        after_cancel.extend([event async for event in stream])
        phases = tuple(recorder.tool_phases())
        self.bridge.apply_interruption(
            delivered_text,
            CancellationMode.DRAIN_CURRENT_UNIT,
            recorder=recorder,
        )

        return ToolCancellationObservation(
            events_before_cancel=before_cancel,
            events_after_cancel=tuple(after_cancel),
            tool_phases_before_cancel=phases_before_cancel,
            tool_phases_after_cancel=phases[len(phases_before_cancel) :],
            committed_assistant_text=self.workflow.history[-1].text,
            inner_stream_close_calls=self.workflow.close_calls,
        )

    async def observe_stream_close_cleanup(self) -> StreamCloseObservation:
        self.workflow.configure("stream_close")
        stream = self.bridge.invoke(AgentTurnInput.from_text("close"), RecordingAgentRecorder())
        await stream.__anext__()
        await stream.aclose()
        return StreamCloseObservation(
            inner_stream_close_calls=self.workflow.close_calls,
            running_work_cancelled=self.workflow.running_work_cancelled,
        )

    async def observe_recorder_transient_cleanup(self) -> RecorderCleanupObservation:
        self.workflow.configure("recorder_cleanup")
        recorder = RecordingAgentRecorder()
        stream = self.bridge.invoke(AgentTurnInput.from_text("cleanup"), recorder)
        await stream.__anext__()
        await stream.aclose()
        entered = tuple(
            record[1][0].unit_id for record in recorder.records if record[0] == "unit_entered"
        )
        exited = tuple(
            record[1][0].unit_id for record in recorder.records if record[0] == "unit_exited"
        )
        return RecorderCleanupObservation(
            entered_cursor_ids=entered,
            exited_cursor_ids=exited,
            transient_items_after_close=len(self.workflow.transient_items),
            inner_stream_close_calls=self.workflow.close_calls,
        )

    async def observe_interruption_history_isolation(
        self, *, prior_user_text: str, prior_assistant_text: str
    ) -> HistoryIsolationObservation:
        self.workflow.seed_prior_turn(prior_user_text, prior_assistant_text)
        before = tuple(self.workflow.history)
        self.workflow.configure("history_isolation")
        recorder = RecordingAgentRecorder()
        stream = self.bridge.invoke(AgentTurnInput.from_text("new turn"), recorder)
        pending_event = asyncio.create_task(stream.__anext__())
        await self.workflow.waiting.wait()
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
            prior_history_after=tuple(self.workflow.history),
        )

    def normalized_state(self) -> NormalizedLifecycleState:
        return NormalizedLifecycleState(
            history=tuple(self.workflow.history),
            active_streams=self.workflow.active_streams,
            transient_items=len(self.workflow.transient_items),
        )

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return self.bridge.snapshot_state()

    def reset(self) -> None:
        self.bridge.reset()


@dataclass
class _ControlledSSEResponse:
    items: list[str | asyncio.Event]

    def __post_init__(self) -> None:
        self.waiting = asyncio.Event()
        self.context_open = False
        self.context_close_calls = 0
        self.line_close_calls = 0
        self.running_work_cancelled = False
        self._index = 0

    async def __aenter__(self) -> Self:
        self.context_open = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.context_open = False
        self.context_close_calls += 1

    def raise_for_status(self) -> None:
        pass

    async def aiter_lines(self) -> AsyncIterator[str]:
        completed = False
        try:
            while self._index < len(self.items):
                item = self.items[self._index]
                self._index += 1
                if isinstance(item, asyncio.Event):
                    self.waiting.set()
                    await item.wait()
                    continue
                yield item
            completed = True
        finally:
            self.running_work_cancelled = not completed
            self.line_close_calls += 1


class _ControlledSSEClient:
    def __init__(self, response: _ControlledSSEResponse) -> None:
        self.response = response
        self.closed = False

    def stream(self, *args: Any, **kwargs: Any) -> _ControlledSSEResponse:
        del args, kwargs
        return self.response

    async def aclose(self) -> None:
        self.closed = True


def _sse(event_type: str, **payload: Any) -> str:
    return "data: " + json.dumps({"type": event_type, **payload})


class _RemoteResponsesLifecycleDriver:
    def __init__(self) -> None:
        self.bridge = RemoteResponsesAPIBridge(
            base_url="http://lifecycle.test",
            model="lifecycle-model",
            api_key="test-key",
        )
        self.response: _ControlledSSEResponse | None = None
        self.normalized_history: list[NormalizedHistoryEntry] = []
        self._prior_response_id: str | None = None

    async def _install(self, items: list[str | asyncio.Event]) -> _ControlledSSEResponse:
        client = self.bridge._client
        if hasattr(client, "aclose"):
            await client.aclose()
        self.response = _ControlledSSEResponse(items)
        self.bridge._client = _ControlledSSEClient(self.response)
        self.bridge._client_closed = False
        return self.response

    async def observe_unknown_event_tolerance(self, *, valid_text: str) -> UnknownEventObservation:
        await self._install(
            [
                "data: not-json",
                _sse("response.future.event", value="ignored"),
                _sse("response.output_text.delta", delta=valid_text),
                _sse("response.completed", response={"id": "resp-unknown"}),
            ]
        )
        events = tuple(
            [
                event
                async for event in self.bridge.invoke(
                    AgentTurnInput.from_text("unknown event"), RecordingAgentRecorder()
                )
            ]
        )
        self.normalized_history = [
            NormalizedHistoryEntry(role="user", text="unknown event"),
            NormalizedHistoryEntry(role="assistant", text=valid_text),
        ]
        return UnknownEventObservation(events=events)

    async def observe_tool_inflight_cancellation(
        self, *, delivered_text: str
    ) -> ToolCancellationObservation:
        gate = asyncio.Event()
        response = await self._install(
            [
                _sse("response.created", response={"id": "resp-tool"}),
                _sse("response.output_text.delta", delta=delivered_text),
                _sse(
                    "response.output_item.added",
                    item={
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "lookup",
                    },
                ),
                gate,
                _sse(
                    "response.output_item.done",
                    item={
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "lookup",
                        "arguments": "{}",
                    },
                ),
                _sse(
                    "response.output_item.done",
                    item={
                        "type": "function_call_output",
                        "call_id": "call-1",
                        "output": "ok",
                    },
                ),
                _sse("response.output_text.delta", delta="must not be delivered"),
                _sse("response.completed", response={"id": "resp-tool"}),
            ]
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
        await response.waiting.wait()
        token.cancel()
        gate.set()
        after_cancel = [await pending_result]
        after_cancel.extend([event async for event in stream])
        phases = tuple(recorder.tool_phases())
        self.bridge.apply_interruption(
            delivered_text,
            CancellationMode.DRAIN_CURRENT_UNIT,
            recorder=recorder,
        )
        replay_items = self.bridge._replay_items or []
        assistant_text = next(
            str(item.get("content", ""))
            for item in replay_items
            if item.get("role") == "assistant"
        ).removesuffix("...")
        self.normalized_history = [
            NormalizedHistoryEntry(role="user", text="use a tool"),
            NormalizedHistoryEntry(role="assistant", text=assistant_text),
        ]

        return ToolCancellationObservation(
            events_before_cancel=tuple(before_cancel),
            events_after_cancel=tuple(after_cancel),
            tool_phases_before_cancel=phases_before_cancel,
            tool_phases_after_cancel=phases[len(phases_before_cancel) :],
            committed_assistant_text=assistant_text,
            inner_stream_close_calls=response.line_close_calls,
        )

    async def observe_stream_close_cleanup(self) -> StreamCloseObservation:
        gate = asyncio.Event()
        response = await self._install(
            [
                _sse("response.created", response={"id": "resp-close"}),
                _sse("response.output_text.delta", delta="partial"),
                gate,
                _sse("response.completed", response={"id": "resp-close"}),
            ]
        )
        stream = self.bridge.invoke(AgentTurnInput.from_text("close"), RecordingAgentRecorder())
        await stream.__anext__()
        await stream.aclose()
        return StreamCloseObservation(
            inner_stream_close_calls=response.line_close_calls,
            running_work_cancelled=response.running_work_cancelled,
        )

    async def observe_recorder_transient_cleanup(self) -> RecorderCleanupObservation:
        gate = asyncio.Event()
        response = await self._install(
            [
                _sse("response.created", response={"id": "resp-cleanup"}),
                _sse(
                    "response.output_item.added",
                    item={
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "lookup",
                    },
                ),
                gate,
                _sse(
                    "response.output_item.done",
                    item={
                        "type": "function_call_output",
                        "call_id": "call-1",
                        "output": "ok",
                    },
                ),
            ]
        )
        recorder = RecordingAgentRecorder()
        stream = self.bridge.invoke(AgentTurnInput.from_text("cleanup"), recorder)
        await stream.__anext__()
        await stream.aclose()
        entered = tuple(
            record[1][0].unit_id for record in recorder.records if record[0] == "unit_entered"
        )
        exited = tuple(
            record[1][0].unit_id for record in recorder.records if record[0] == "unit_exited"
        )
        return RecorderCleanupObservation(
            entered_cursor_ids=entered,
            exited_cursor_ids=exited,
            transient_items_after_close=self._transient_items(),
            inner_stream_close_calls=response.line_close_calls,
        )

    async def observe_interruption_history_isolation(
        self, *, prior_user_text: str, prior_assistant_text: str
    ) -> HistoryIsolationObservation:
        await self._install(
            [
                _sse("response.output_text.delta", delta=prior_assistant_text),
                _sse("response.completed", response={"id": "resp-prior"}),
            ]
        )
        async for _ in self.bridge.invoke(
            AgentTurnInput.from_text(prior_user_text), RecordingAgentRecorder()
        ):
            pass
        self._prior_response_id = "resp-prior"
        self.normalized_history = [
            NormalizedHistoryEntry(role="user", text=prior_user_text),
            NormalizedHistoryEntry(role="assistant", text=prior_assistant_text),
        ]
        before = self._project_prior_history()

        gate = asyncio.Event()
        response = await self._install(
            [
                _sse("response.created", response={"id": "resp-current"}),
                gate,
                _sse("response.output_text.delta", delta="must not be delivered"),
                _sse("response.completed", response={"id": "resp-current"}),
            ]
        )
        recorder = RecordingAgentRecorder()
        stream = self.bridge.invoke(AgentTurnInput.from_text("new turn"), recorder)
        pending_event = asyncio.create_task(stream.__anext__())
        await response.waiting.wait()
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

    def _project_prior_history(self) -> tuple[NormalizedHistoryEntry, ...]:
        if self.bridge._last_completed_response_id != self._prior_response_id:
            return ()
        return tuple(self.normalized_history[:2])

    def _transient_items(self) -> int:
        values = (
            self.bridge._replay_items,
            self.bridge._pending_interruption_note,
            self.bridge._pending_assistant_history_items,
            self.bridge._pending_turn_metadata,
        )
        return sum(bool(value) for value in values)

    def normalized_state(self) -> NormalizedLifecycleState:
        active_streams = int(bool(self.response and self.response.context_open))
        return NormalizedLifecycleState(
            history=tuple(self.normalized_history),
            active_streams=active_streams,
            transient_items=self._transient_items(),
        )

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return self.bridge.snapshot_state()

    def reset(self) -> None:
        self.bridge.reset()
        self.bridge._client_closed = True
        self.normalized_history.clear()
        self._prior_response_id = None


class TestGenericWorkflowBridgeLifecycleScenarios(BridgeLifecycleScenarioSuite):
    driver_factory = _GenericWorkflowLifecycleDriver
    applicable_scenarios = GENERIC_WORKFLOW_SCENARIOS


class TestRemoteResponsesAPIBridgeLifecycleScenarios(BridgeLifecycleScenarioSuite):
    driver_factory = _RemoteResponsesLifecycleDriver


def test_credential_free_suite_scenarios_match_execution_matrix() -> None:
    matrix_path = Path(__file__).with_name("bridge-lifecycle-matrix.json")
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    execution = matrix["driver_execution"]["bridges"]

    assert execution["generic_workflow"]["status"] == "wired"
    assert set(execution["generic_workflow"]["scenarios"]) == (
        TestGenericWorkflowBridgeLifecycleScenarios.applicable_scenarios
    )
    assert execution["remote_responses_api"]["status"] == "wired"
    assert set(execution["remote_responses_api"]["scenarios"]) == (
        TestRemoteResponsesAPIBridgeLifecycleScenarios.applicable_scenarios
    )
