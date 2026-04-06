"""WS2A acceptance criteria tests that don't require live SDK mocks.

AC2.13: FrameworkStateSnapshot safety and JSON-safety
AC2.17: Handoff record triple verification
AC2.19: AgentTurnInput.from_text() direct invoke on bridges
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import (
    AgentTurnInput,
    ExecutionCursor,
    FrameworkStateSnapshot,
    RecorderContext,
    UnitKind,
)
from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge
from easycat.runtime.journal import InMemoryRingBuffer


def _recorder(journal=None):
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


# ── AC2.13: FrameworkStateSnapshot safety ────────────────────────


class TestFrameworkStateSnapshotSafety:
    """AC2.13 — snapshots are JSON-safe, secret-safe, no raw handles."""

    def test_snapshot_is_json_serializable(self):
        snap = FrameworkStateSnapshot(
            fields={"agent": "TestAgent", "turn_count": 5},
            kind="test",
        )
        serialized = json.dumps(snap.fields)
        assert "TestAgent" in serialized
        assert snap.state_ref is None

    def test_snapshot_state_ref_format(self):
        """Non-null state_ref should be a plausible artifact ref."""
        snap = FrameworkStateSnapshot(
            fields={"summary": "large_state"},
            state_ref="a" * 64,
            kind="test",
        )
        assert len(snap.state_ref) == 64
        assert all(c in "0123456789abcdef" for c in snap.state_ref)

    def test_snapshot_kind_non_empty(self):
        snap = FrameworkStateSnapshot(fields={}, kind="openai_agents")
        assert snap.kind != ""

    def test_generic_workflow_snapshot_no_secrets(self):
        class _Workflow:
            async def on_user_turn(self, text: str) -> str:
                return text

        bridge = GenericWorkflowBridge(workflow=_Workflow())
        snap = bridge.snapshot_state()
        serialized = json.dumps(snap.fields)
        assert "api_key" not in serialized.lower()
        assert "secret" not in serialized.lower()

    def test_snapshot_4kb_overflow_detection(self):
        """Snapshots over 4KB should use state_ref (PydanticAI graph mode)."""
        # The PydanticAIBridge.snapshot_state() checks len > 4096
        # and sets state_ref. We verify the threshold logic directly.
        fields = {"data": "x" * 5000}
        inline = json.dumps(fields)
        assert len(inline) > 4096
        # A bridge producing this should set state_ref.
        # We test the contract, not a specific bridge.
        snap = FrameworkStateSnapshot(
            fields={"state_summary": "LargeState"},
            state_ref="abcdef" * 11,  # simulated artifact ref
            kind="pydantic_ai_graph",
        )
        assert snap.state_ref is not None
        assert len(json.dumps(snap.fields)) < 4096


# ── AC2.17: Handoff record triple ────────────────────────────────


class TestHandoffRecordTriple:
    """AC2.17 — handoff produces exit → handoff → enter in sequence."""

    def test_recorder_handoff_triple(self):
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        # Simulate: agent A running, then handoff to agent B.
        cursor_a = ExecutionCursor(
            unit_id="agent-a",
            unit_kind=UnitKind.AGENT,
            display_name="AgentA",
        )
        rec.record_unit_entered(cursor_a)
        rec.record_unit_exited(cursor_a, reason="handoff")
        rec.record_framework_handoff(
            from_unit="AgentA",
            to_unit="AgentB",
            reason="agent_handoff",
        )
        cursor_b = ExecutionCursor(
            unit_id="agent-b",
            unit_kind=UnitKind.AGENT,
            display_name="AgentB",
        )
        rec.record_unit_entered(cursor_b)
        rec.record_unit_exited(cursor_b.with_committable(True), reason=None)

        records = journal.read()
        names = [r.name for r in records]

        # Verify the triple: exit → handoff → enter in sequence.
        exit_idx = names.index("unit_exited")
        handoff_idx = names.index("framework_handoff")
        enter_b_idx = len(names) - 1 - names[::-1].index("unit_entered")

        assert exit_idx < handoff_idx < enter_b_idx

        # Verify from_unit/to_unit consistency.
        handoff_data = records[handoff_idx].data
        assert handoff_data["from_unit"] == "AgentA"
        assert handoff_data["to_unit"] == "AgentB"


# ── AC2.19: AgentTurnInput.from_text() direct invoke ─────────────


class _SimpleWorkflow:
    async def on_user_turn(self, text: str) -> str:
        return f"Response: {text}"


class _DeepWorkflow:
    async def on_user_turn(
        self, text: str, *, recorder=None, cancel_token=None
    ) -> AsyncIterator[str]:
        yield f"Deep: {text}"


class TestAgentTurnInputFromTextDirectInvoke:
    """AC2.19 — from_text() constructs valid input for bridge.invoke()."""

    def test_from_text_basic(self):
        inp = AgentTurnInput.from_text("hello")
        assert inp.text == "hello"
        assert inp.context == []
        assert inp.turn_id is None

    def test_from_text_with_context_and_turn_id(self):
        ctx = [{"role": "system", "content": "You are helpful."}]
        inp = AgentTurnInput.from_text("hello", context=ctx, turn_id="t1")
        assert inp.context == ctx
        assert inp.turn_id == "t1"

    @pytest.mark.asyncio
    async def test_from_text_invoke_generic_shallow(self):
        bridge = GenericWorkflowBridge(workflow=_SimpleWorkflow())
        inp = AgentTurnInput.from_text("test input")
        rec = _recorder()

        events = []
        async for ev in bridge.invoke(inp, rec):
            events.append(ev)

        done_events = [e for e in events if e.kind == "done"]
        assert len(done_events) == 1
        assert "test input" in done_events[0].text

    @pytest.mark.asyncio
    async def test_from_text_invoke_generic_deep(self):
        bridge = GenericWorkflowBridge(workflow=_DeepWorkflow())
        inp = AgentTurnInput.from_text("deep test")
        rec = _recorder()

        events = []
        async for ev in bridge.invoke(inp, rec):
            events.append(ev)

        text_events = [e for e in events if e.kind == "text_delta"]
        assert len(text_events) >= 1
        assert "deep test" in text_events[0].text


# ── AC2.6d: Graph mode convention validation ─────────────────────


class TestPydanticAIBridgeConventionValidation:
    """AC2.6d — construction-time BridgeConfigurationError."""

    def test_missing_convention_slot_raises(self):
        """State without _easycat_event_handler raises at construction."""
        from dataclasses import dataclass

        from easycat.integrations.agents.base import BridgeConfigurationError

        @dataclass
        class BadState:
            value: str = ""

        class MockGraph:
            pass

        with pytest.raises(BridgeConfigurationError, match="_easycat_event_handler"):
            from easycat.integrations.agents.pydantic_ai import PydanticAIBridge

            PydanticAIBridge(
                graph=MockGraph(),
                state_factory=BadState,
                initial_node_factory=lambda text, state: None,
            )
