from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    CancellationMode,
    CommitRule,
    ExecutionCursor,
    FrameworkStateSnapshot,
    InterruptionPlan,
    UnitKind,
    run_interruption_journal_protocol,
)
from easycat.testing import AgentBridgeContractSuite, RecordingAgentRecorder
from tests.contracts.provider_surface_matrix import PROVIDER_SURFACE_CONTRACTS

pytestmark = [
    pytest.mark.contract,
    pytest.mark.agent_bridge,
    pytest.mark.surface_agent,
    pytest.mark.provider("offline-fake"),
]
REPO_ROOT = Path(__file__).resolve().parents[2]


class _ContractBridge:
    COMMITTABLE_BOUNDARIES = {UnitKind.AGENT: CommitRule.BETWEEN_TURNS}  # noqa: RUF012 test fake uses shared class fixture

    def __init__(self) -> None:
        self.history: list[str] = []
        self.interruptions: list[tuple[str, CancellationMode]] = []

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token=None,
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
        recorder.record_state_snapshot("snapshot-ref", payload=b'{"history_len":0}')
        self.history.append(turn_input.text)
        # Cursor / handoff / state-snapshot transitions are journaled via the
        # recorder above; bridges never mirror them onto the stream, which
        # carries only text / tool / done events.
        yield AgentBridgeEvent(kind="text_delta", text="hello")
        yield AgentBridgeEvent(kind="tool_started", tool_name="lookup", call_id="call-1")
        yield AgentBridgeEvent(
            kind="tool_result",
            tool_name="lookup",
            call_id="call-1",
            result="ok",
        )
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


class TestAgentBridgeContractSuite(AgentBridgeContractSuite):
    """Run the shipped bridge-author kit suite against the offline fake.

    The event-grammar and journal-protocol assertions live in
    :class:`easycat.testing.AgentBridgeContractSuite` so this file and the
    installable kit cannot drift; only fake-specific exact-sequence checks
    are added below.
    """

    provider_factory = _ContractBridge

    async def test_fake_event_order_and_recorder_writes(
        self, provider: _ContractBridge, recorder: RecordingAgentRecorder
    ) -> None:
        events = [
            event async for event in provider.invoke(AgentTurnInput.from_text("hi"), recorder)
        ]

        # The stream carries only text / tool / done events; cursor, handoff
        # and state-snapshot transitions live solely in the AgentRecorder
        # journal.
        assert [event.kind for event in events] == [
            "text_delta",
            "tool_started",
            "tool_result",
            "done",
        ]
        assert events[1].tool_name == "lookup"
        assert events[2].result == "ok"
        # The authoritative cursor / handoff / state records are journaled.
        assert "unit_entered" in recorder.kinds()
        assert recorder.tool_phases() == ["start", "result"]
        assert "handoff" in recorder.kinds()
        assert "state_snapshot" in recorder.kinds()
        assert "unit_exited" in recorder.kinds()

    async def test_fake_interruption_records_exact_journal_sequence(
        self, provider: _ContractBridge, recorder: RecordingAgentRecorder
    ) -> None:
        provider.apply_interruption(
            "hello",
            CancellationMode.IMMEDIATE_STOP,
            recorder=recorder,
            caused_by_signal_id="sig-1",
        )

        assert provider.interruptions == [("hello", CancellationMode.IMMEDIATE_STOP)]
        assert recorder.kinds() == [
            "cancellation_boundary",
            "state_snapshot",
            "state_committed",
            "state_snapshot",
        ]
        assert recorder.records[2][2] == {"pre_state_ref": "pre", "post_state_ref": "post"}

    async def test_fake_snapshot_and_reset_are_json_safe(self, provider: _ContractBridge) -> None:
        provider.history.append("hello")

        snapshot = provider.snapshot_state()
        provider.reset()

        assert snapshot.fields == {"history": ["hello"]}
        assert snapshot.kind == "fake"
        assert provider.history == []


def test_interruption_protocol_swallows_post_commit_journal_failure() -> None:
    """A step-4b journal failure must not escape or undo the mutation.

    Once ``record_state_committed`` has succeeded and the mutation has been
    applied, a degraded journal raising on the post-snapshot /
    ``record_cancellation_boundary`` write must be logged and swallowed —
    the mutation already stands, so re-raising would surface a spurious
    error for a barge-in that actually completed.
    """

    class _FailingBoundaryRecorder(RecordingAgentRecorder):
        def record_cancellation_boundary(
            self,
            mode: CancellationMode,
            reason: str | None = None,
            caused_by_signal_id: str | None = None,
        ) -> None:
            raise RuntimeError("journal degraded")

    recorder = _FailingBoundaryRecorder()
    applied: list[InterruptionPlan] = []
    plan = InterruptionPlan(
        mutation_kind="interrupt_truncate",
        pre_state_ref="pre",
        post_state_ref="post",
    )

    # Must not raise even though record_cancellation_boundary blows up.
    run_interruption_journal_protocol(
        plan,
        CancellationMode.IMMEDIATE_STOP,
        recorder,
        "sig-1",
        serialize_state=lambda: b"{}",
        apply_mutation=applied.append,
    )

    # The mutation applied and the commit was journaled before the failure.
    assert applied == [plan]
    record_kinds = recorder.kinds()
    assert "state_committed" in record_kinds
    # The post-mutation snapshot write precedes the failing boundary write,
    # so it is recorded; the boundary write raised and was swallowed.
    assert record_kinds == ["state_snapshot", "state_committed", "state_snapshot"]


def test_interruption_protocol_skips_mutation_when_commit_fails() -> None:
    """A degraded journal at the commit step skips the mutation entirely."""

    class _FailingCommitRecorder(RecordingAgentRecorder):
        def record_state_committed(
            self,
            mutation_kind: str,
            pre_state_ref: str | None = None,
            post_state_ref: str | None = None,
        ) -> None:
            raise RuntimeError("journal degraded")

    recorder = _FailingCommitRecorder()
    applied: list[InterruptionPlan] = []
    plan = InterruptionPlan(
        mutation_kind="interrupt_truncate",
        pre_state_ref="pre",
        post_state_ref="post",
    )

    run_interruption_journal_protocol(
        plan,
        CancellationMode.IMMEDIATE_STOP,
        recorder,
        "sig-1",
        serialize_state=lambda: b"{}",
        apply_mutation=applied.append,
    )

    # Commit failed → mutation must not have been applied.
    assert applied == []
