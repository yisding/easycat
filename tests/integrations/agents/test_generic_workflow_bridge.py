"""AC2.6e, AC2.6f, AC2A.7b: GenericWorkflowBridge shallow + deep mode."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import (
    AgentTurnInput,
    BridgeInputError,
    CancellationMode,
    ExecutionCursor,
    RecorderContext,
    ShallowModeInterruptionError,
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


class _ShallowWorkflow:
    """Minimal shallow-mode workflow."""

    async def on_user_turn(self, text: str) -> str:
        return f"Echo: {text}"


class _ShallowStreamingWorkflow:
    """Shallow-mode with streaming."""

    async def on_user_turn(self, text: str) -> str:
        return f"Echo: {text}"

    async def on_user_turn_streaming(self, text: str) -> AsyncIterator[str]:
        for word in text.split():
            yield word + " "


class _DeepWorkflow:
    """Deep-mode workflow that calls recorder methods."""

    async def on_user_turn(
        self,
        text: str,
        *,
        recorder: JournalAgentRecorder | None = None,
        cancel_token=None,
    ) -> AsyncIterator[str]:
        if recorder is not None:
            cursor = ExecutionCursor(
                unit_id="classifier",
                unit_kind=UnitKind.SPECIALIST,
                display_name="IntentClassifier",
            )
            recorder.record_unit_entered(cursor)
            recorder.record_unit_exited(cursor, reason="done")

            recorder.record_tool_call(phase="start", name="lookup")
            recorder.record_tool_call(phase="result", name="lookup")

        yield f"Deep: {text}"


class _InterruptibleShallowWorkflow:
    """Shallow workflow that opts into interruption."""

    async def on_user_turn(self, text: str) -> str:
        return f"Echo: {text}"

    def apply_interruption(self, delivered_text: str, mode: CancellationMode) -> None:
        pass  # Accept interruption.


class _NoOnUserTurn:
    """Invalid workflow — no on_user_turn."""

    pass


# ── Tests ────────────────────────────────────────────────────────


class TestShallowMode:
    """AC2.6e — shallow mode."""

    def test_detected_as_shallow(self):
        bridge = GenericWorkflowBridge(workflow=_ShallowWorkflow())
        assert not bridge.deep_mode

    @pytest.mark.asyncio
    async def test_invoke_yields_text(self):
        bridge = GenericWorkflowBridge(workflow=_ShallowWorkflow())
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            events.append(ev)

        text_events = [e for e in events if e.kind == "text_delta"]
        done_events = [e for e in events if e.kind == "done"]
        assert len(text_events) >= 1
        assert len(done_events) == 1
        assert "hi" in done_events[0].text

    @pytest.mark.asyncio
    async def test_invoke_streaming_variant(self):
        bridge = GenericWorkflowBridge(workflow=_ShallowStreamingWorkflow())
        rec = _recorder()
        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hello world"), rec):
            events.append(ev)
        text_events = [e for e in events if e.kind == "text_delta"]
        assert len(text_events) >= 2  # "hello " and "world "

    @pytest.mark.asyncio
    async def test_journal_has_workflow_node_cursor(self):
        bridge = GenericWorkflowBridge(workflow=_ShallowWorkflow())
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)
        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            pass

        records = journal.read()
        names = [r.name for r in records]
        assert "unit_entered" in names
        assert "unit_exited" in names


class TestDeepMode:
    """AC2.6f — deep mode."""

    def test_detected_as_deep(self):
        bridge = GenericWorkflowBridge(workflow=_DeepWorkflow())
        assert bridge.deep_mode

    @pytest.mark.asyncio
    async def test_invoke_passes_recorder(self):
        bridge = GenericWorkflowBridge(workflow=_DeepWorkflow())
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            events.append(ev)

        text_events = [e for e in events if e.kind == "text_delta"]
        assert len(text_events) >= 1

        # Deep mode records should be in the journal.
        records = journal.read()
        names = [r.name for r in records]
        assert "unit_entered" in names  # outer workflow + classifier
        assert "tool_phase_changed" in names


class TestInterruption:
    """AC2A.7b — ShallowModeInterruptionError."""

    def test_shallow_without_apply_raises(self):
        bridge = GenericWorkflowBridge(workflow=_ShallowWorkflow())
        with pytest.raises(ShallowModeInterruptionError, match="shallow mode"):
            bridge.apply_interruption("hello", CancellationMode.IMMEDIATE_STOP)

    def test_shallow_with_explicit_opt_in(self):
        bridge = GenericWorkflowBridge(workflow=_InterruptibleShallowWorkflow())
        # Should not raise.
        bridge.apply_interruption("hello", CancellationMode.IMMEDIATE_STOP)

    def test_deep_without_apply_does_not_raise(self):
        bridge = GenericWorkflowBridge(workflow=_DeepWorkflow())
        # Should not raise — falls back to cancel_token.
        bridge.apply_interruption("hello", CancellationMode.IMMEDIATE_STOP)


class TestConstruction:
    def test_no_on_user_turn_raises(self):
        with pytest.raises(BridgeInputError, match="on_user_turn"):
            GenericWorkflowBridge(workflow=_NoOnUserTurn())

    def test_custom_display_name(self):
        bridge = GenericWorkflowBridge(workflow=_ShallowWorkflow(), display_name="MyWorkflow")
        snap = bridge.snapshot_state()
        assert snap.fields["display_name"] == "MyWorkflow"

    def test_reset_delegates(self):
        class _Resettable:
            reset_called = False

            async def on_user_turn(self, text: str) -> str:
                return text

            def reset(self):
                self.reset_called = True

        w = _Resettable()
        bridge = GenericWorkflowBridge(workflow=w)
        bridge.reset()
        assert w.reset_called


class TestCommittableBoundaries:
    """AC2.16 — COMMITTABLE_BOUNDARIES published."""

    def test_boundaries_present(self):
        assert hasattr(GenericWorkflowBridge, "COMMITTABLE_BOUNDARIES")
        assert len(GenericWorkflowBridge.COMMITTABLE_BOUNDARIES) > 0


class _SlowStreamingWorkflow:
    """Shallow streaming workflow that blocks until cancelled."""

    async def on_user_turn(self, text: str) -> str:
        return f"Echo: {text}"

    async def on_user_turn_streaming(self, text: str) -> AsyncIterator[str]:
        yield "first "
        await asyncio.sleep(10)
        yield "never"


class TestStreamingStructuredOutput:
    """Finding 2 — streaming shallow mode leaves structured_output as None.

    Streamed chunks are inherently unstructured text. Emitting the joined
    text as ``structured_output`` would merely duplicate the ``done`` event's
    ``text`` field (and could surface a partial value on barge-in cancel), so
    the bridge leaves it ``None`` — matching deep-mode streaming.
    """

    @pytest.mark.asyncio
    async def test_streaming_variant_leaves_structured_output_none(self):
        bridge = GenericWorkflowBridge(workflow=_ShallowStreamingWorkflow())
        rec = _recorder()
        done = None
        text = ""
        async for ev in bridge.invoke(AgentTurnInput.from_text("hello world"), rec):
            if ev.kind == "text_delta":
                text += ev.text
            elif ev.kind == "done":
                done = ev
        assert done is not None
        # Streaming chunks are unstructured: text is delivered but
        # structured_output stays None rather than duplicating the text.
        assert text == "hello world "
        assert done.text == "hello world "
        assert done.structured_output is None


class TestCursorCleanupOnCancel:
    """Finding 1 — cursor stack stays balanced on GeneratorExit/cancel."""

    @pytest.mark.asyncio
    async def test_shallow_streaming_aclose_balances_cursor(self):
        bridge = GenericWorkflowBridge(workflow=_SlowStreamingWorkflow())
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)
        gen = bridge.invoke(AgentTurnInput.from_text("hi"), rec)
        # Consume the first chunk, then abort mid-stream (barge-in / timeout).
        first = await gen.__anext__()
        assert first.kind == "text_delta"
        await gen.aclose()
        # The workflow cursor must have been closed despite the
        # GeneratorExit injected by aclose(), leaving a balanced stack.
        assert rec._open_cursors == []
        names = [r.name for r in journal.read()]
        assert names.count("unit_entered") == names.count("unit_exited")
