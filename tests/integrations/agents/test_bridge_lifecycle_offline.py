"""Run the internal bridge lifecycle scenario suite against a model bridge."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import ClassVar

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    CancellationMode,
    CommitRule,
    ExecutionCursor,
    ExternalAgentBridge,
    FrameworkStateSnapshot,
    UnitKind,
)
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
    pytest.mark.provider("offline-lifecycle-model"),
]


@dataclass(frozen=True)
class _ProviderEvent:
    kind: str
    text: str = ""
    call_id: str = ""


class _ScriptedInnerStream:
    """Provider stream with an exact close probe and controllable gates."""

    def __init__(self, items: list[object]) -> None:
        self._items = items
        self._index = 0
        self._closed = False
        self.waiting = asyncio.Event()
        self.close_calls = 0
        self.running_work_cancelled = False

    def __aiter__(self) -> _ScriptedInnerStream:
        return self

    async def __anext__(self) -> object:
        if self._closed or self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        if isinstance(item, asyncio.Event):
            self.waiting.set()
            await item.wait()
            return await self.__anext__()
        return item

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.close_calls += 1
        self.running_work_cancelled = self._index < len(self._items)


class _OfflineLifecycleBridge:
    """Deterministic bridge that models provider translation and cleanup."""

    COMMITTABLE_BOUNDARIES: ClassVar[dict[UnitKind | str, CommitRule]] = {
        UnitKind.AGENT: CommitRule.BETWEEN_TURNS,
        UnitKind.TOOL_CALL: CommitRule.BETWEEN_PHASES,
    }

    def __init__(self) -> None:
        self.history: list[NormalizedHistoryEntry] = []
        self.active_streams = 0
        self.transient_items: set[str] = set()
        self.stream_opened = asyncio.Event()
        self.last_inner_stream: _ScriptedInnerStream | None = None
        self._next_script: list[object] = []
        self._turn_sequence = 0
        self._current_assistant_index: int | None = None

    def configure(self, items: list[object]) -> None:
        assert self.active_streams == 0
        self._next_script = list(items)
        self.last_inner_stream = None
        self.stream_opened.clear()

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        self._turn_sequence += 1
        turn_id = f"agent-{self._turn_sequence}"
        agent_cursor = ExecutionCursor(unit_id=turn_id, unit_kind=UnitKind.AGENT)
        tool_cursor: ExecutionCursor | None = None
        inner = _ScriptedInnerStream(self._next_script)
        self.last_inner_stream = inner
        self.stream_opened.set()
        self.active_streams += 1
        self.transient_items.add(turn_id)
        self._current_assistant_index = None
        current_text = ""
        turn_committed = False
        recorder.record_unit_entered(agent_cursor)

        try:
            async for raw_event in inner:
                if not isinstance(raw_event, _ProviderEvent):
                    continue
                if raw_event.kind == "text":
                    if cancel_token is not None and cancel_token.is_cancelled:
                        continue
                    current_text += raw_event.text
                    yield AgentBridgeEvent(kind="text_delta", text=raw_event.text)
                elif raw_event.kind == "tool_start":
                    tool_cursor = ExecutionCursor(
                        unit_id=f"tool-{self._turn_sequence}",
                        unit_kind=UnitKind.TOOL_CALL,
                        parent_unit_id=turn_id,
                    )
                    self.transient_items.add(tool_cursor.unit_id)
                    recorder.record_unit_entered(tool_cursor)
                    recorder.record_tool_call("start", "lookup", call_id=raw_event.call_id)
                    yield AgentBridgeEvent(
                        kind="tool_started",
                        tool_name="lookup",
                        call_id=raw_event.call_id,
                    )
                elif raw_event.kind == "tool_result" and tool_cursor is not None:
                    recorder.record_tool_call(
                        "result",
                        "lookup",
                        result_ref="result-ref",
                        call_id=raw_event.call_id,
                    )
                    recorder.record_unit_exited(tool_cursor.with_committable(True))
                    self.transient_items.discard(tool_cursor.unit_id)
                    tool_cursor = None
                    stop_after_yield = bool(cancel_token and cancel_token.is_cancelled)
                    yield AgentBridgeEvent(
                        kind="tool_result",
                        tool_name="lookup",
                        call_id=raw_event.call_id,
                        result=raw_event.text,
                    )
                    if stop_after_yield:
                        break
                elif raw_event.kind == "done":
                    self._commit_turn(turn_input.text, current_text)
                    turn_committed = True
                    yield AgentBridgeEvent(kind="done", text=current_text)
                    break
        finally:
            await self._finalize_invoke(
                inner=inner,
                recorder=recorder,
                agent_cursor=agent_cursor,
                tool_cursor=tool_cursor,
                turn_id=turn_id,
                user_text=turn_input.text,
                current_text=current_text,
                turn_committed=turn_committed,
            )

    async def _finalize_invoke(
        self,
        *,
        inner: _ScriptedInnerStream,
        recorder: AgentRecorder,
        agent_cursor: ExecutionCursor,
        tool_cursor: ExecutionCursor | None,
        turn_id: str,
        user_text: str,
        current_text: str,
        turn_committed: bool,
    ) -> None:
        if not turn_committed and current_text:
            self._commit_turn(user_text, current_text)
        if tool_cursor is not None:
            recorder.safe_exit_cursor(tool_cursor)
            self.transient_items.discard(tool_cursor.unit_id)
        recorder.safe_exit_cursor(agent_cursor)
        self.transient_items.discard(turn_id)
        self.active_streams -= 1
        await inner.aclose()

    def _commit_turn(self, user_text: str, assistant_text: str) -> None:
        self.history.append(NormalizedHistoryEntry(role="user", text=user_text))
        self.history.append(NormalizedHistoryEntry(role="assistant", text=assistant_text))
        self._current_assistant_index = len(self.history) - 1

    def seed_prior_turn(self, user_text: str, assistant_text: str) -> None:
        self.history = [
            NormalizedHistoryEntry(role="user", text=user_text),
            NormalizedHistoryEntry(role="assistant", text=assistant_text),
        ]
        self._current_assistant_index = None

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return FrameworkStateSnapshot(
            kind="offline-lifecycle-model",
            fields={
                "history": [{"role": entry.role, "text": entry.text} for entry in self.history],
                "active_streams": self.active_streams,
                "transient_items": sorted(self.transient_items),
            },
        )

    def apply_interruption(
        self,
        delivered_text: str,
        mode: CancellationMode,
        recorder: AgentRecorder | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        if self._current_assistant_index is not None:
            self.history[self._current_assistant_index] = NormalizedHistoryEntry(
                role="assistant", text=delivered_text
            )
        if recorder is not None:
            recorder.record_cancellation_boundary(
                mode,
                reason="offline lifecycle model",
                caused_by_signal_id=caused_by_signal_id,
            )

    def replace_last_assistant_text(self, text: str) -> None:
        for index in range(len(self.history) - 1, -1, -1):
            if self.history[index].role == "assistant":
                self.history[index] = NormalizedHistoryEntry(role="assistant", text=text)
                return

    def append_interruption_note(self, note: str) -> None:
        self.history.append(NormalizedHistoryEntry(role="system", text=note))

    def reset(self) -> None:
        assert self.active_streams == 0
        self.history.clear()
        self.transient_items.clear()
        self._current_assistant_index = None


class _OfflineLifecycleDriver:
    def __init__(self) -> None:
        self.bridge = _OfflineLifecycleBridge()

    async def observe_unknown_event_tolerance(self, *, valid_text: str) -> UnknownEventObservation:
        self.bridge.configure(
            [
                object(),
                _ProviderEvent(kind="future_event"),
                _ProviderEvent(kind="text", text=valid_text),
                _ProviderEvent(kind="done"),
            ]
        )
        recorder = RecordingAgentRecorder()
        events = tuple(
            [
                event
                async for event in self.bridge.invoke(
                    AgentTurnInput.from_text("unknown event"), recorder
                )
            ]
        )
        return UnknownEventObservation(events=events)

    async def observe_tool_inflight_cancellation(
        self, *, delivered_text: str
    ) -> ToolCancellationObservation:
        gate = asyncio.Event()
        token = CancelToken()
        self.bridge.configure(
            [
                _ProviderEvent(kind="text", text=delivered_text),
                _ProviderEvent(kind="tool_start", call_id="call-1"),
                gate,
                _ProviderEvent(kind="tool_result", text="ok", call_id="call-1"),
                _ProviderEvent(kind="text", text="must not be delivered"),
                _ProviderEvent(kind="done"),
            ]
        )
        recorder = RecordingAgentRecorder()
        stream = self.bridge.invoke(AgentTurnInput.from_text("use a tool"), recorder, token)
        before_cancel = (await stream.__anext__(), await stream.__anext__())
        assert self.bridge.last_inner_stream is not None
        pending_result = asyncio.create_task(stream.__anext__())
        await self.bridge.last_inner_stream.waiting.wait()
        token.cancel()
        gate.set()
        after_cancel = [await pending_result]
        after_cancel.extend([event async for event in stream])
        self.bridge.apply_interruption(
            delivered_text,
            CancellationMode.DRAIN_CURRENT_UNIT,
            recorder=recorder,
        )
        assistant_entries = [
            entry.text for entry in self.bridge.history if entry.role == "assistant"
        ]

        return ToolCancellationObservation(
            events_before_cancel=before_cancel,
            events_after_cancel=tuple(after_cancel),
            tool_phases=tuple(recorder.tool_phases()),
            committed_assistant_text=assistant_entries[-1],
            inner_stream_close_calls=self.bridge.last_inner_stream.close_calls,
        )

    async def observe_stream_close_cleanup(self) -> StreamCloseObservation:
        gate = asyncio.Event()
        self.bridge.configure(
            [
                _ProviderEvent(kind="text", text="partial"),
                gate,
                _ProviderEvent(kind="done"),
            ]
        )
        recorder = RecordingAgentRecorder()
        stream = self.bridge.invoke(AgentTurnInput.from_text("close"), recorder)
        await stream.__anext__()
        await stream.aclose()
        assert self.bridge.last_inner_stream is not None
        return StreamCloseObservation(
            inner_stream_close_calls=self.bridge.last_inner_stream.close_calls,
            running_work_cancelled=self.bridge.last_inner_stream.running_work_cancelled,
        )

    async def observe_recorder_transient_cleanup(self) -> RecorderCleanupObservation:
        gate = asyncio.Event()
        self.bridge.configure(
            [
                _ProviderEvent(kind="tool_start", call_id="call-1"),
                gate,
                _ProviderEvent(kind="tool_result", text="ok", call_id="call-1"),
            ]
        )
        recorder = RecordingAgentRecorder()
        stream = self.bridge.invoke(AgentTurnInput.from_text("cleanup"), recorder)
        await stream.__anext__()
        await stream.aclose()
        assert self.bridge.last_inner_stream is not None
        entered = tuple(
            record[1][0].unit_id for record in recorder.records if record[0] == "unit_entered"
        )
        exited = tuple(
            record[1][0].unit_id for record in recorder.records if record[0] == "unit_exited"
        )
        return RecorderCleanupObservation(
            entered_cursor_ids=entered,
            exited_cursor_ids=exited,
            transient_items_after_close=len(self.bridge.transient_items),
            inner_stream_close_calls=self.bridge.last_inner_stream.close_calls,
        )

    async def observe_interruption_history_isolation(
        self, *, prior_user_text: str, prior_assistant_text: str
    ) -> HistoryIsolationObservation:
        gate = asyncio.Event()
        self.bridge.seed_prior_turn(prior_user_text, prior_assistant_text)
        before = tuple(self.bridge.history)
        self.bridge.configure(
            [
                gate,
                _ProviderEvent(kind="text", text="must not be delivered"),
                _ProviderEvent(kind="done"),
            ]
        )
        recorder = RecordingAgentRecorder()
        stream = self.bridge.invoke(AgentTurnInput.from_text("new turn"), recorder)
        pending_event = asyncio.create_task(stream.__anext__())
        await self.bridge.stream_opened.wait()
        assert self.bridge.last_inner_stream is not None
        await self.bridge.last_inner_stream.waiting.wait()
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
            history_before=before,
            history_after=tuple(self.bridge.history),
        )

    def normalized_state(self) -> NormalizedLifecycleState:
        return NormalizedLifecycleState(
            history=tuple(self.bridge.history),
            active_streams=self.bridge.active_streams,
            transient_items=len(self.bridge.transient_items),
        )

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return self.bridge.snapshot_state()

    def reset(self) -> None:
        self.bridge.reset()


class TestOfflineBridgeLifecycleScenarios(BridgeLifecycleScenarioSuite):
    """Run all shared WS3.1 lifecycle rows without optional SDK extras."""

    driver_factory = _OfflineLifecycleDriver


def test_offline_lifecycle_model_is_an_external_agent_bridge() -> None:
    assert isinstance(_OfflineLifecycleBridge(), ExternalAgentBridge)
