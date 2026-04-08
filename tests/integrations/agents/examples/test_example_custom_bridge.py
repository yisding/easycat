"""Example 5: Custom ExternalAgentBridge from scratch.

Mirrors plan appendix Example 5 — a minimal bridge that wraps a raw
inference backend (mocked here) and emits ``AgentBridgeEvent`` yields
with full recorder calls.

This fixture runs end-to-end without any third-party SDK.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents._bridge_adapter_shim import BridgeAdapterShim
from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    CancellationMode,
    CommitRule,
    ExecutionCursor,
    FrameworkStateSnapshot,
    RecorderContext,
    UnitKind,
)
from easycat.runtime.journal import InMemoryRingBuffer
from easycat.runtime.records import ErrorInfo

# ── Custom bridge (matches plan appendix Example 5) ─────────────


class DirectChatBridge:
    """Minimal custom bridge: mocked chat completions with no framework."""

    COMMITTABLE_BOUNDARIES = {UnitKind.AGENT: CommitRule.BETWEEN_TURNS}

    def __init__(self, *, model: str, system: str) -> None:
        self._model = model
        self._system = system
        self._history: list[dict[str, str]] = []

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        cursor = ExecutionCursor(
            unit_id=f"turn-{turn_input.turn_id or 'x'}",
            unit_kind=UnitKind.AGENT,
            display_name="DirectChat",
            parent_unit_id=None,
            sequence=len(self._history) + 1,
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        recorder.record_unit_entered(cursor)

        self._history.append({"role": "user", "content": turn_input.text})

        # Simulate streaming response.
        response_text = f"You said: {turn_input.text}"
        try:
            for word in response_text.split():
                if cancel_token and cancel_token.is_cancelled():
                    break
                yield AgentBridgeEvent(kind="text_delta", text=word + " ")

            self._history.append({"role": "assistant", "content": response_text})
            recorder.record_unit_exited(cursor.with_committable(True), reason="stream_complete")
            yield AgentBridgeEvent(kind="done", text=response_text)
        except Exception as exc:
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            recorder.record_unit_exited(cursor, reason="error")
            raise

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return FrameworkStateSnapshot(
            fields={
                "model": self._model,
                "turn_count": len(self._history) // 2,
            },
            kind="custom_chat",
        )

    def apply_interruption(self, delivered_text: str, mode: CancellationMode) -> None:
        if self._history and self._history[-1]["role"] == "assistant":
            if delivered_text:
                self._history[-1]["content"] = delivered_text + "..."
            else:
                self._history.pop()

    def reset(self) -> None:
        self._history.clear()


# ── Tests ────────────────────────────────────────────────────────


def _recorder(journal: InMemoryRingBuffer | None = None) -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


class TestCustomBridgeExample:
    """Plan appendix Example 5 — custom ExternalAgentBridge."""

    @pytest.mark.asyncio
    async def test_invoke_streams_text_and_done(self):
        bridge = DirectChatBridge(model="test-model", system="You are helpful.")
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        events = []
        async for ev in bridge.invoke(AgentTurnInput.from_text("hello"), rec):
            events.append(ev)

        text_events = [e for e in events if e.kind == "text_delta"]
        done_events = [e for e in events if e.kind == "done"]
        assert len(text_events) >= 1
        assert len(done_events) == 1
        assert "hello" in done_events[0].text

    @pytest.mark.asyncio
    async def test_journal_has_agent_cursor_pair(self):
        bridge = DirectChatBridge(model="test-model", system="You are helpful.")
        journal = InMemoryRingBuffer(capacity=1000)
        rec = _recorder(journal)

        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), rec):
            pass

        records = journal.read()
        names = [r.name for r in records]
        assert "unit_entered" in names
        assert "unit_exited" in names

    def test_snapshot_state(self):
        bridge = DirectChatBridge(model="gpt-5.2", system="test")
        snap = bridge.snapshot_state()
        assert snap.kind == "custom_chat"
        assert snap.fields["model"] == "gpt-5.2"

    def test_apply_interruption_truncates_history(self):
        bridge = DirectChatBridge(model="test", system="test")
        bridge._history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "Hello, how can I help?"},
        ]
        bridge.apply_interruption("Hello", mode=CancellationMode.IMMEDIATE_STOP)
        assert bridge._history[-1]["content"] == "Hello..."

    def test_apply_interruption_removes_empty_delivery(self):
        bridge = DirectChatBridge(model="test", system="test")
        bridge._history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        bridge.apply_interruption("", mode=CancellationMode.IMMEDIATE_STOP)
        assert len(bridge._history) == 1

    def test_reset_clears_history(self):
        bridge = DirectChatBridge(model="test", system="test")
        bridge._history = [{"role": "user", "content": "hi"}]
        bridge.reset()
        assert len(bridge._history) == 0

    def test_committable_boundaries_published(self):
        assert DirectChatBridge.COMMITTABLE_BOUNDARIES
        assert UnitKind.AGENT in DirectChatBridge.COMMITTABLE_BOUNDARIES

    def test_wrappable_in_shim(self):
        """Custom bridge can be wrapped in BridgeAdapterShim for Session compat."""
        bridge = DirectChatBridge(model="test", system="test")
        shim = BridgeAdapterShim(bridge)
        assert shim.bridge is bridge
