"""Example 4: GenericWorkflowBridge in deep mode.

Mirrors plan appendix Example 4 — custom orchestration that accepts a
``recorder`` parameter and calls ``recorder.record_*`` methods to emit
structured records from inside its own code.

This fixture runs end-to-end without any third-party SDK.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import (
    AgentRecorder,
    AgentTurnInput,
    ExecutionCursor,
    RecorderContext,
    UnitKind,
)
from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge
from easycat.runtime.journal import InMemoryRingBuffer

# ── Workflow (matches plan appendix Example 4) ───────────────────


class SupportOrchestrator:
    """Custom orchestration with opt-in journal visibility."""

    def __init__(self) -> None:
        self._history: list[tuple[str, str]] = []

    async def on_user_turn(
        self,
        text: str,
        *,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[str]:
        # Step 1: intent classification.
        intent_cursor = ExecutionCursor(
            unit_id="intent-classifier",
            unit_kind=UnitKind.SPECIALIST,
            display_name="IntentClassifier",
            parent_unit_id=None,
            sequence=1,
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        recorder.record_unit_entered(intent_cursor)
        intent = await self._classify_intent(text)
        recorder.record_unit_exited(intent_cursor, reason="classified")

        # Step 2: tool call if the intent needs it.
        if intent == "weather":
            recorder.record_tool_call(
                phase="start",
                name="get_weather",
                args_ref=None,
                result_ref=None,
            )
            result = await self._get_weather(text)
            recorder.record_tool_call(
                phase="result",
                name="get_weather",
                args_ref=None,
                result_ref=None,
            )
        else:
            result = "I can't help with that yet."

        # Step 3: stream the response text.
        response_cursor = ExecutionCursor(
            unit_id="response-writer",
            unit_kind=UnitKind.AGENT,
            display_name="ResponseWriter",
            parent_unit_id=None,
            sequence=2,
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        recorder.record_unit_entered(response_cursor)
        for word in result.split():
            if cancel_token and cancel_token.is_cancelled():
                break
            yield word + " "
        recorder.record_unit_exited(
            response_cursor.with_committable(True),
            reason="stream_complete",
        )

        self._history.append((text, result))

    async def _classify_intent(self, text: str) -> str:
        return "weather" if "weather" in text.lower() else "other"

    async def _get_weather(self, text: str) -> str:
        return "It's 24°C and sunny."

    def reset(self) -> None:
        self._history.clear()


# ── Tests ────────────────────────────────────────────────────────


def _recorder(journal: InMemoryRingBuffer | None = None) -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


class TestGenericDeepExample:
    """Plan appendix Example 4 — GenericWorkflowBridge deep mode."""

    def test_construction_picks_deep_mode(self):
        bridge = GenericWorkflowBridge(workflow=SupportOrchestrator())
        assert bridge.deep_mode

    @pytest.mark.asyncio
    async def test_invoke_yields_text_chunks(self):
        bridge = GenericWorkflowBridge(workflow=SupportOrchestrator())
        rec = _recorder()

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("what's the weather?"), rec):
            events.append(ev)

        text_events = [e for e in events if e.kind == "text_delta"]
        done_events = [e for e in events if e.kind == "done"]
        assert len(text_events) >= 2  # multiple words streamed
        assert len(done_events) == 1

    @pytest.mark.asyncio
    async def test_journal_contains_nested_records(self):
        """Deep mode emits inner unit entries + tool calls under the outer cursor."""
        bridge = GenericWorkflowBridge(workflow=SupportOrchestrator())
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        async for _ in bridge.invoke(AgentTurnInput.from_text("what's the weather?"), rec):
            pass

        records = journal.read()
        names = [r.name for r in records]

        # Outer workflow cursor.
        assert names.count("unit_entered") >= 3  # outer + classifier + response-writer
        assert names.count("unit_exited") >= 3

        # Tool calls recorded by the workflow code.
        tool_records = [r for r in records if r.name == "tool_phase_changed"]
        assert len(tool_records) == 2  # start + result
        phases = [r.data["phase"] for r in tool_records]
        assert "start" in phases
        assert "result" in phases

    @pytest.mark.asyncio
    async def test_non_weather_intent_skips_tool(self):
        bridge = GenericWorkflowBridge(workflow=SupportOrchestrator())
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        async for _ in bridge.invoke(AgentTurnInput.from_text("tell me a joke"), rec):
            pass

        records = journal.read()
        tool_records = [r for r in records if r.name == "tool_phase_changed"]
        assert len(tool_records) == 0

    def test_snapshot_state_reports_deep(self):
        bridge = GenericWorkflowBridge(workflow=SupportOrchestrator())
        snap = bridge.snapshot_state()
        assert snap.fields["mode"] == "deep"
