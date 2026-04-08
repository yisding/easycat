"""Example 3: GenericWorkflowBridge in shallow mode.

Mirrors plan appendix Example 3 — custom orchestration code with a
single ``on_user_turn(text) -> str`` method.  The bridge wraps the whole
turn in one opaque ``workflow_node`` cursor and yields text deltas.

This fixture runs end-to-end without any third-party SDK.
"""

from __future__ import annotations

import pytest

from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import AgentTurnInput, RecorderContext
from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge
from easycat.runtime.journal import InMemoryRingBuffer

# ── Workflow (matches plan appendix Example 3) ───────────────────


class SupportOrchestrator:
    """Custom multi-agent orchestration.  Could use any backend internally."""

    def __init__(self) -> None:
        self._history: list[tuple[str, str]] = []

    async def on_user_turn(self, text: str) -> str:
        response = await self._dispatch(text)
        self._history.append((text, response))
        return response

    async def _dispatch(self, text: str) -> str:
        return f"I'll help you with: {text}"

    def reset(self) -> None:
        self._history.clear()


# ── Tests ────────────────────────────────────────────────────────


def _recorder(journal: InMemoryRingBuffer | None = None) -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


class TestGenericShallowExample:
    """Plan appendix Example 3 — GenericWorkflowBridge shallow mode."""

    def test_construction_picks_shallow_mode(self):
        bridge = GenericWorkflowBridge(workflow=SupportOrchestrator())
        assert not bridge.deep_mode

    @pytest.mark.asyncio
    async def test_invoke_returns_text_and_done(self):
        bridge = GenericWorkflowBridge(workflow=SupportOrchestrator())
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("billing issue"), rec):
            events.append(ev)

        text_events = [e for e in events if e.kind == "text_delta"]
        done_events = [e for e in events if e.kind == "done"]
        assert len(text_events) >= 1
        assert len(done_events) == 1
        assert "billing issue" in done_events[0].text

    @pytest.mark.asyncio
    async def test_journal_has_single_workflow_node_cursor(self):
        bridge = GenericWorkflowBridge(workflow=SupportOrchestrator())
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        async for _ in bridge.invoke(AgentTurnInput.from_text("hello"), rec):
            pass

        records = journal.read()
        names = [r.name for r in records]
        assert "unit_entered" in names
        assert "unit_exited" in names
        # Shallow mode: zero tool-call records.
        tool_records = [r for r in records if r.name == "tool_call"]
        assert len(tool_records) == 0

    @pytest.mark.asyncio
    async def test_reset_clears_workflow_history(self):
        workflow = SupportOrchestrator()
        bridge = GenericWorkflowBridge(workflow=workflow)
        rec = _recorder()

        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            pass

        assert len(workflow._history) == 1
        bridge.reset()
        assert len(workflow._history) == 0

    def test_snapshot_state_reports_shallow(self):
        bridge = GenericWorkflowBridge(workflow=SupportOrchestrator())
        snap = bridge.snapshot_state()
        assert snap.kind == "generic_workflow"
        assert snap.fields["mode"] == "shallow"
        assert snap.fields["display_name"] == "SupportOrchestrator"
