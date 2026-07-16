"""AC2.13b, AC2.13c: AgentRecorder invariants, unit() context manager, journal writes."""

from __future__ import annotations

import pytest

from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import (
    CancellationMode,
    ExecutionCursor,
    NullAgentRecorder,
    RecorderContext,
    RecorderInvariantError,
    UnitKind,
)
from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.records import ErrorInfo


@pytest.fixture
def journal():
    return InMemoryRingBuffer(capacity=1000)


@pytest.fixture
def recorder(journal):
    return JournalAgentRecorder(
        journal=journal,
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


def _cursor(unit_id: str = "u1", kind: UnitKind = UnitKind.AGENT) -> ExecutionCursor:
    return ExecutionCursor(unit_id=unit_id, unit_kind=kind, display_name="Test")


class TestRecorderLifecycle:
    def test_enter_exit_writes_to_journal(self, recorder, journal):
        c = _cursor()
        recorder.record_unit_entered(c)
        recorder.record_unit_exited(c, reason="done")

        records = journal.read()
        assert len(records) == 2
        assert records[0].name == "unit_entered"
        assert records[1].name == "unit_exited"
        assert records[0].data["run_id"] == "r1"
        assert records[1].data["run_id"] == "r1"

    def test_tool_call_recorded(self, recorder, journal):
        recorder.record_tool_call(phase="start", name="get_weather")
        recorder.record_tool_call(phase="result", name="get_weather")

        records = journal.read()
        assert len(records) == 2
        assert records[0].data["phase"] == "start"
        assert records[1].data["phase"] == "result"

    def test_handoff_recorded(self, recorder, journal):
        recorder.record_framework_handoff(from_unit="AgentA", to_unit="AgentB", reason="handoff")
        records = journal.read()
        assert len(records) == 1
        assert records[0].data["from_unit"] == "AgentA"
        assert records[0].data["to_unit"] == "AgentB"

    def test_cancellation_boundary_recorded(self, recorder, journal):
        recorder.record_cancellation_boundary(
            CancellationMode.IMMEDIATE_STOP,
            reason="barge_in",
            caused_by_signal_id="sig-1",
        )
        records = journal.read()
        assert len(records) == 1
        assert records[0].data["caused_by_signal_id"] == "sig-1"

    def test_framework_error_recorded(self, recorder, journal):
        recorder.record_framework_error(ErrorInfo(type="RuntimeError", message="fail"))
        records = journal.read()
        assert len(records) == 1
        assert records[0].error is not None
        assert records[0].error.type == "RuntimeError"
        assert records[0].data["run_id"] == "r1"

    def test_state_snapshot_recorded(self, recorder, journal):
        recorder.record_state_snapshot(ref="abc123")
        records = journal.read()
        assert len(records) == 1
        assert records[0].data["state_ref"] == "abc123"


class TestRecorderNoop:
    """Recorder with journal=None should silently no-op."""

    def test_noop_when_no_journal(self):
        rec = JournalAgentRecorder(
            journal=None,
            artifact_store=None,
            context=RecorderContext(run_id="r1", session_id="s1"),
        )
        c = _cursor()
        rec.record_unit_entered(c)
        rec.record_unit_exited(c)
        rec.record_tool_call(phase="start", name="tool")
        rec.record_framework_handoff(from_unit="A", to_unit="B")
        # No exception — all calls are no-ops.

    def test_null_recorder_preserves_supplied_context(self):
        context = RecorderContext(
            run_id="null",
            session_id="session-1",
            turn_id="turn-1",
            mcp_servers=("weather",),
        )

        recorder = NullAgentRecorder(context)

        assert recorder.context is context


class TestRecorderContext:
    """AC2.13a — context is accessible and frozen."""

    def test_context_accessible(self, recorder):
        ctx = recorder.context
        assert ctx.run_id == "r1"
        assert ctx.session_id == "s1"
        assert ctx.turn_id == "t1"

    def test_context_frozen(self, recorder):
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            recorder.context.run_id = "new"  # type: ignore[misc]

    def test_mcp_servers_default_empty(self, recorder):
        assert recorder.context.mcp_servers == ()


class TestUnitContextManager:
    """AC2.13b — unit() guarantees paired enter/exit."""

    def test_normal_exit(self, recorder, journal):
        c = _cursor()
        with recorder.unit(c) as yielded:
            assert yielded.unit_id == "u1"
        records = journal.read()
        assert records[0].name == "unit_entered"
        assert records[1].name == "unit_exited"
        # Exit should have committable=True (commit_on_exit default).
        assert records[1].data["committable"] is True

    def test_exception_exit(self, recorder, journal):
        c = _cursor()
        with pytest.raises(ValueError):
            with recorder.unit(c):
                raise ValueError("boom")

        records = journal.read()
        assert len(records) == 2
        assert records[1].name == "unit_exited"
        assert records[1].data["exit_reason"] == "exception:ValueError"

    def test_no_commit_on_exit(self, recorder, journal):
        c = _cursor()
        with recorder.unit(c, commit_on_exit=False):
            pass
        records = journal.read()
        assert records[1].data["committable"] is False


class TestSafeExitCursor:
    """safe_exit_cursor() closes cursors defensively, swallowing failures."""

    def test_closes_open_cursor(self, recorder, journal):
        c = _cursor()
        recorder.record_unit_entered(c)
        recorder.safe_exit_cursor(c)
        records = journal.read()
        assert [r.name for r in records] == ["unit_entered", "unit_exited"]
        assert records[1].data["exit_reason"] == "error"

    def test_swallows_recorder_invariant_error(self, recorder):
        # Exiting a cursor that was never entered violates the stack
        # invariant; safe_exit_cursor must swallow it rather than letting it
        # mask the original (cancellation) exception in a bridge's error arm.
        c = _cursor()
        recorder.safe_exit_cursor(c)  # must not raise

    def test_honors_explicit_reason(self, recorder, journal):
        c = _cursor()
        recorder.record_unit_entered(c)
        recorder.safe_exit_cursor(c.with_committable(True), reason=None)
        records = journal.read()
        assert records[1].data["exit_reason"] is None
        assert records[1].data["committable"] is True


class TestTurnCursorContextManager:
    """turn_cursor() centralizes the per-turn enter/error/cancel/commit ordering."""

    def test_normal_exit_commits(self, recorder, journal):
        c = _cursor()
        with recorder.turn_cursor(c) as yielded:
            assert yielded.unit_id == "u1"
        records = journal.read()
        assert [r.name for r in records] == ["unit_entered", "unit_exited"]
        assert records[-1].data["committable"] is True
        assert records[-1].data["exit_reason"] is None

    def test_exception_records_framework_error_and_error_exit(self, recorder, journal):
        c = _cursor()
        with pytest.raises(ValueError):
            with recorder.turn_cursor(c):
                raise ValueError("boom")

        records = journal.read()
        names = [r.name for r in records]
        assert "framework_error" in names
        exits = [r for r in records if r.name == "unit_exited"]
        assert len(exits) == 1
        assert exits[0].data["exit_reason"] == "error"

    def test_base_exception_safe_exits_without_framework_error(self, recorder, journal):
        c = _cursor()
        with pytest.raises(GeneratorExit):
            with recorder.turn_cursor(c):
                raise GeneratorExit()

        records = journal.read()
        names = [r.name for r in records]
        assert "framework_error" not in names
        exits = [r for r in records if r.name == "unit_exited"]
        assert len(exits) == 1
        # safe_exit_cursor default reason is "error".
        assert exits[0].data["exit_reason"] == "error"

    def test_cancelled_error_is_base_exception_path(self, recorder, journal):
        import asyncio

        c = _cursor()
        with pytest.raises(asyncio.CancelledError):
            with recorder.turn_cursor(c):
                raise asyncio.CancelledError()

        records = journal.read()
        assert "framework_error" not in [r.name for r in records]
        assert [r.name for r in records] == ["unit_entered", "unit_exited"]

    def test_baseexception_cleanup_fault_is_swallowed(self, recorder):
        # If the cancel-cleanup exit violates the recorder's stack invariant
        # (here: a nested cursor left open so the stack top mismatches), the
        # fault must be swallowed rather than masking the original cancellation.
        outer = _cursor("outer")
        with pytest.raises(GeneratorExit):
            with recorder.turn_cursor(outer):
                recorder.record_unit_entered(_cursor("inner"))
                raise GeneratorExit()


class TestRecorderInvariantEnforcement:
    """AC2.13c — catches deep-mode misuse."""

    def test_exit_without_enter_raises(self, recorder):
        c = _cursor()
        with pytest.raises(RecorderInvariantError, match="without a matching"):
            recorder.record_unit_exited(c)

    def test_exit_wrong_cursor_raises(self, recorder):
        c1 = _cursor("u1")
        c2 = _cursor("u2")
        recorder.record_unit_entered(c1)
        recorder.record_unit_entered(c2)
        with pytest.raises(RecorderInvariantError, match="top of the cursor stack"):
            recorder.record_unit_exited(c1)

    def test_duplicate_unit_id_raises(self, recorder):
        c = _cursor("u1")
        recorder.record_unit_entered(c)
        recorder.record_unit_exited(c)
        with pytest.raises(RecorderInvariantError, match="Duplicate unit_id"):
            recorder.record_unit_entered(c)
