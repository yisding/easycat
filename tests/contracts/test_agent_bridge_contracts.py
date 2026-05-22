from __future__ import annotations

import importlib
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    CancellationMode,
    CommitRule,
    ExecutionCursor,
    ExternalAgentBridge,
    FrameworkStateSnapshot,
    RecorderContext,
    UnitKind,
)
from easycat.runtime.records import ErrorInfo
from tests.contracts.provider_surface_matrix import PROVIDER_SURFACE_CONTRACTS

pytestmark = [
    pytest.mark.contract,
    pytest.mark.agent_bridge,
    pytest.mark.surface_agent,
    pytest.mark.provider("offline-fake"),
]


class _RecordingAgentRecorder:
    def __init__(self) -> None:
        self.context = RecorderContext(run_id="run-1", session_id="session-1", turn_id="turn-1")
        self.records: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def record_unit_entered(self, cursor: ExecutionCursor) -> None:
        self.records.append(("unit_entered", (cursor,), {}))

    def record_unit_exited(self, cursor: ExecutionCursor, reason: str | None = None) -> None:
        self.records.append(("unit_exited", (cursor,), {"reason": reason}))

    @contextmanager
    def unit(
        self,
        cursor: ExecutionCursor,
        *,
        commit_on_exit: bool = True,
    ) -> Iterator[ExecutionCursor]:
        del commit_on_exit
        self.record_unit_entered(cursor)
        try:
            yield cursor
        finally:
            self.record_unit_exited(cursor)

    def record_tool_call(
        self,
        phase: str,
        name: str,
        args_ref: str | None = None,
        result_ref: str | None = None,
        call_id: str | None = None,
    ) -> None:
        self.records.append(
            (
                "tool_call",
                (phase, name),
                {"args_ref": args_ref, "result_ref": result_ref, "call_id": call_id},
            )
        )

    def record_state_snapshot(self, ref: str, *, payload: bytes | None = None) -> str:
        self.records.append(("state_snapshot", (ref,), {"payload": payload}))
        return ref

    def record_framework_handoff(
        self,
        from_unit: str | None,
        to_unit: str,
        reason: str | None = None,
    ) -> None:
        self.records.append(("handoff", (from_unit, to_unit), {"reason": reason}))

    def record_cancellation_boundary(
        self,
        mode: CancellationMode,
        reason: str | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        self.records.append(
            (
                "cancellation_boundary",
                (mode,),
                {"reason": reason, "caused_by_signal_id": caused_by_signal_id},
            )
        )

    def record_framework_error(self, error: ErrorInfo) -> None:
        self.records.append(("framework_error", (error,), {}))

    def record_state_committed(
        self,
        mutation_kind: str,
        pre_state_ref: str | None = None,
        post_state_ref: str | None = None,
    ) -> None:
        self.records.append(
            (
                "state_committed",
                (mutation_kind,),
                {"pre_state_ref": pre_state_ref, "post_state_ref": post_state_ref},
            )
        )

    def record_interruption_apply_failed(
        self,
        mutation_kind: str,
        pre_state_ref: str | None = None,
        post_state_ref: str | None = None,
        failure_error: ErrorInfo | None = None,
    ) -> None:
        self.records.append(
            (
                "interruption_apply_failed",
                (mutation_kind,),
                {
                    "pre_state_ref": pre_state_ref,
                    "post_state_ref": post_state_ref,
                    "failure_error": failure_error,
                },
            )
        )


class _ContractBridge:
    COMMITTABLE_BOUNDARIES = {UnitKind.AGENT: CommitRule.BETWEEN_TURNS}

    def __init__(self) -> None:
        self.history: list[str] = []
        self.interruptions: list[tuple[str, CancellationMode]] = []

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token=None,  # noqa: ANN001
    ) -> AsyncIterator[AgentBridgeEvent]:
        del cancel_token
        cursor = ExecutionCursor(unit_id="agent-1", unit_kind=UnitKind.AGENT)
        tool_cursor = ExecutionCursor(
            unit_id="tool-1",
            unit_kind=UnitKind.TOOL_CALL,
            parent_unit_id="agent-1",
        )
        recorder.record_unit_entered(cursor)
        recorder.record_unit_entered(tool_cursor)
        recorder.record_tool_call("start", "lookup", call_id="call-1")
        recorder.record_tool_call("result", "lookup", result_ref="result-ref", call_id="call-1")
        recorder.record_framework_handoff("agent-1", "agent-2", reason="handoff")
        snapshot = FrameworkStateSnapshot(fields={"history_len": len(self.history)}, kind="fake")
        recorder.record_state_snapshot("snapshot-ref", payload=b'{"history_len":0}')
        self.history.append(turn_input.text)
        yield AgentBridgeEvent(kind="cursor_entered", cursor=cursor)
        yield AgentBridgeEvent(kind="cursor_entered", cursor=tool_cursor)
        yield AgentBridgeEvent(kind="text_delta", text="hello")
        yield AgentBridgeEvent(kind="tool_started", tool_name="lookup", call_id="call-1")
        yield AgentBridgeEvent(
            kind="tool_result",
            tool_name="lookup",
            call_id="call-1",
            result="ok",
        )
        yield AgentBridgeEvent(kind="handoff", from_unit="agent-1", to_unit="agent-2")
        yield AgentBridgeEvent(kind="state_snapshot", snapshot=snapshot)
        yield AgentBridgeEvent(kind="cursor_exited", cursor=tool_cursor)
        yield AgentBridgeEvent(kind="cursor_exited", cursor=cursor)
        yield AgentBridgeEvent(kind="done", text="hello")
        recorder.record_unit_exited(tool_cursor.with_committable(True), reason=None)
        recorder.record_unit_exited(cursor.with_committable(True), reason=None)

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return FrameworkStateSnapshot(fields={"history": list(self.history)}, kind="fake")

    def apply_interruption(
        self,
        delivered_text: str,
        mode: CancellationMode,
        recorder: AgentRecorder | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        self.interruptions.append((delivered_text, mode))
        if recorder is not None:
            recorder.record_cancellation_boundary(
                mode,
                reason="contract",
                caused_by_signal_id=caused_by_signal_id,
            )
            recorder.record_state_snapshot("pre", payload=b"{}")
            recorder.record_state_committed(
                "interrupt_truncate",
                pre_state_ref="pre",
                post_state_ref="post",
            )
            recorder.record_state_snapshot("post", payload=b"{}")

    def replace_last_assistant_text(self, text: str) -> None:
        if self.history:
            self.history[-1] = text

    def append_interruption_note(self, note: str) -> None:
        self.history.append(note)

    def reset(self) -> None:
        self.history.clear()


def test_agent_bridge_contract_matrix_has_rows_for_supported_bridges() -> None:
    rows = [row for row in PROVIDER_SURFACE_CONTRACTS if row.surface == "agent_bridge"]

    assert {row.provider for row in rows} == {
        "openai-agents",
        "pydantic-ai",
        "generic-workflow",
        "remote-responses-api",
        "langchain",
        "langgraph",
        "llama-agents",
    }
    assert all(
        row.contract_path == "tests/contracts/test_agent_bridge_contracts.py" for row in rows
    )
    assert all(row.expected_skip_reason for row in rows if row.required_extra)


def test_agent_bridge_contract_matrix_adapters_are_importable_or_expected_skip() -> None:
    rows = [row for row in PROVIDER_SURFACE_CONTRACTS if row.surface == "agent_bridge"]

    for row in rows:
        module_name, _, class_name = row.adapter.rpartition(".")
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            assert row.required_extra
            assert row.expected_skip_reason
            continue
        assert getattr(module, class_name)


async def test_agent_bridge_contract_event_grammar_and_recorder_writes() -> None:
    bridge = _ContractBridge()
    recorder = _RecordingAgentRecorder()

    assert isinstance(bridge, ExternalAgentBridge)
    events = [event async for event in bridge.invoke(AgentTurnInput.from_text("hi"), recorder)]

    assert [event.kind for event in events] == [
        "cursor_entered",
        "cursor_entered",
        "text_delta",
        "tool_started",
        "tool_result",
        "handoff",
        "state_snapshot",
        "cursor_exited",
        "cursor_exited",
        "done",
    ]
    assert events[0].cursor is not None
    assert events[1].cursor is not None
    assert events[3].tool_name == "lookup"
    assert events[4].result == "ok"
    assert events[5].from_unit == "agent-1"
    assert events[5].to_unit == "agent-2"
    assert events[6].snapshot is not None
    assert events[6].snapshot.fields == {"history_len": 0}
    _assert_cursor_events_are_paired(events)
    assert ("unit_entered", (events[0].cursor,), {}) in recorder.records
    assert ("unit_entered", (events[1].cursor,), {}) in recorder.records
    assert _recorded_tool_phases(recorder) == ["start", "result"]
    assert any(record[0] == "handoff" for record in recorder.records)
    assert any(record[0] == "state_snapshot" for record in recorder.records)
    assert any(record[0] == "unit_exited" for record in recorder.records)


def test_agent_bridge_contract_interruption_records_boundary_and_commit() -> None:
    bridge = _ContractBridge()
    recorder = _RecordingAgentRecorder()

    bridge.apply_interruption(
        "hello",
        CancellationMode.IMMEDIATE_STOP,
        recorder=recorder,
        caused_by_signal_id="sig-1",
    )

    assert bridge.interruptions == [("hello", CancellationMode.IMMEDIATE_STOP)]
    assert [record[0] for record in recorder.records] == [
        "cancellation_boundary",
        "state_snapshot",
        "state_committed",
        "state_snapshot",
    ]
    assert recorder.records[2][2] == {"pre_state_ref": "pre", "post_state_ref": "post"}


def test_agent_bridge_contract_snapshot_and_reset_are_json_safe() -> None:
    bridge = _ContractBridge()
    bridge.history.append("hello")

    snapshot = bridge.snapshot_state()
    bridge.reset()

    assert snapshot.fields == {"history": ["hello"]}
    assert snapshot.kind == "fake"
    assert bridge.history == []


def _recorded_tool_phases(recorder: _RecordingAgentRecorder) -> list[str]:
    return [record[1][0] for record in recorder.records if record[0] == "tool_call"]


def _assert_cursor_events_are_paired(events: list[AgentBridgeEvent]) -> None:
    stack: list[str] = []
    for event in events:
        if event.kind == "cursor_entered":
            assert event.cursor is not None
            stack.append(event.cursor.unit_id)
        elif event.kind == "cursor_exited":
            assert event.cursor is not None
            assert stack
            assert stack.pop() == event.cursor.unit_id
    assert stack == []
