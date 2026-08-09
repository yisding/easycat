"""AC2.2, AC2.3: Protocol types, transition records, and construction errors."""

from __future__ import annotations

import pytest

from easycat.integrations.agents._text_stream import AgentTextStream
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentTurnInput,
    BridgeInputError,
    CancellationMode,
    CommitRule,
    ExecutionCursor,
    FrameworkStateSnapshot,
    RecorderContext,
    ShallowModeInterruptionError,
    UnitKind,
)
from easycat.runtime.records import (
    FrameworkCancellationBoundaryReached,
    FrameworkHandoff,
    FrameworkStateCommitted,
    FrameworkToolPhaseChanged,
    FrameworkTransitionRecord,
    FrameworkUnitEntered,
    FrameworkUnitExited,
    InterruptionApplyFailed,
)


class TestProtocolTypes:
    """AC2.2 — all protocol types importable and constructible."""

    def test_execution_cursor(self):
        c = ExecutionCursor(
            unit_id="u1",
            unit_kind=UnitKind.AGENT,
            display_name="TestAgent",
        )
        assert c.unit_id == "u1"
        assert c.committable is False

    def test_cursor_with_committable(self):
        c = ExecutionCursor(unit_id="u1", unit_kind=UnitKind.AGENT)
        c2 = c.with_committable(True)
        assert c.committable is False
        assert c2.committable is True
        assert c.unit_id == c2.unit_id

    def test_agent_turn_input_from_text(self):
        inp = AgentTurnInput.from_text("hello", turn_id="t1")
        assert inp.text == "hello"
        assert inp.turn_id == "t1"
        assert inp.context == []

    def test_agent_turn_input_with_context(self):
        ctx = [{"role": "user", "content": "prior"}]
        inp = AgentTurnInput.from_text("hello", context=ctx)
        assert len(inp.context) == 1

    def test_agent_bridge_event(self):
        e = AgentBridgeEvent(kind="text_delta", text="hi")
        assert e.kind == "text_delta"
        assert e.text == "hi"

    def test_agent_bridge_text_replacement(self):
        event = AgentBridgeEvent(kind="text_replace", text="correct", part_index=1)

        assert event.part_index == 1

    def test_agent_bridge_text_replacement_requires_an_index(self):
        with pytest.raises(ValueError, match="non-negative part_index"):
            AgentBridgeEvent(kind="text_replace", text="correct")

    def test_agent_text_stream_replaces_indexed_parts_in_response_order(self):
        stream = AgentTextStream()
        stream.apply(AgentBridgeEvent(kind="text_replace", text="later", part_index=4))
        stream.apply(AgentBridgeEvent(kind="text_replace", text="first ", part_index=1))
        stream.apply(AgentBridgeEvent(kind="text_delta", text="done", part_index=4))
        update = stream.apply(
            AgentBridgeEvent(kind="text_replace", text="corrected", part_index=4)
        )

        assert update is not None
        assert update.text == "first corrected"

    def test_agent_text_stream_rejects_mixed_flat_and_indexed_events(self):
        stream = AgentTextStream()
        stream.apply(AgentBridgeEvent(kind="text_delta", text="flat"))

        with pytest.raises(ValueError, match="cannot mix"):
            stream.apply(AgentBridgeEvent(kind="text_replace", text="indexed", part_index=0))

    def test_framework_state_snapshot(self):
        s = FrameworkStateSnapshot(
            fields={"agent": "TestAgent"},
            kind="test",
        )
        assert s.fields["agent"] == "TestAgent"
        assert s.state_ref is None

    def test_recorder_context(self):
        ctx = RecorderContext(
            run_id="r1",
            session_id="s1",
            turn_id="t1",
            mcp_servers=("stdio://foo",),
        )
        assert ctx.mcp_servers == ("stdio://foo",)

    def test_cancellation_mode_enum(self):
        assert CancellationMode.IMMEDIATE_STOP.value == "immediate_stop"
        assert CancellationMode.DRAIN_CURRENT_UNIT.value == "drain_current_unit"
        assert CancellationMode.DRAIN_TO_COMMIT_POINT.value == "drain_to_commit_point"

    def test_commit_rule_enum(self):
        assert CommitRule.BETWEEN_PHASES.value == "between_phases"
        assert CommitRule.BETWEEN_TURNS.value == "between_turns"

    def test_unit_kind_enum(self):
        assert UnitKind.AGENT.value == "agent"
        assert UnitKind.WORKFLOW_NODE.value == "workflow_node"
        assert UnitKind.TOOL_CALL.value == "tool_call"


class TestTransitionRecordTypes:
    """AC2.3 — seven transition record types exist and are instantiable."""

    def test_framework_unit_entered(self):
        r = FrameworkUnitEntered(
            sequence=1,
            session_id="s",
            unit_id="u1",
            unit_kind="agent",
            display_name="TestAgent",
        )
        assert r.direction == "enter"
        assert r.unit_id == "u1"

    def test_framework_unit_exited(self):
        r = FrameworkUnitExited(
            sequence=2,
            session_id="s",
            unit_id="u1",
            unit_kind="agent",
            exit_reason="completed",
        )
        assert r.direction == "exit"

    def test_framework_state_committed(self):
        r = FrameworkStateCommitted(
            sequence=3,
            session_id="s",
            mutation_kind="interrupt_truncate",
        )
        assert r.mutation_kind == "interrupt_truncate"

    def test_framework_handoff(self):
        r = FrameworkHandoff(
            sequence=4,
            session_id="s",
            from_unit="AgentA",
            to_unit="AgentB",
            transition_kind="agent_handoff",
        )
        assert r.from_unit == "AgentA"
        assert r.to_unit == "AgentB"

    def test_framework_tool_phase_changed(self):
        r = FrameworkToolPhaseChanged(
            sequence=5,
            session_id="s",
            phase="start",
            tool_name="get_weather",
        )
        assert r.phase == "start"

    def test_framework_cancellation_boundary_reached(self):
        r = FrameworkCancellationBoundaryReached(
            sequence=6,
            session_id="s",
            cancellation_mode="immediate_stop",
            caused_by_signal_id="sig-1",
        )
        assert r.caused_by_signal_id == "sig-1"

    def test_interruption_apply_failed(self):
        from easycat.runtime.records import ErrorInfo

        r = InterruptionApplyFailed(
            sequence=7,
            session_id="s",
            mutation_kind="interrupt_truncate",
            failure_error=ErrorInfo(type="RuntimeError", message="fail"),
        )
        assert r.failure_error is not None

    def test_all_extend_framework_transition(self):
        for cls in [
            FrameworkUnitEntered,
            FrameworkUnitExited,
            FrameworkStateCommitted,
            FrameworkHandoff,
            FrameworkToolPhaseChanged,
            FrameworkCancellationBoundaryReached,
            InterruptionApplyFailed,
        ]:
            r = cls(sequence=1, session_id="s")
            assert isinstance(r, FrameworkTransitionRecord)


class TestErrorClasses:
    def test_bridge_input_error(self):
        with pytest.raises(BridgeInputError):
            raise BridgeInputError("test")

    def test_shallow_mode_interruption_error(self):
        with pytest.raises(ShallowModeInterruptionError):
            raise ShallowModeInterruptionError("test")
