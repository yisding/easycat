"""Tests for AgentRunner as an ExternalAgentBridge.

``AgentRunner`` wraps a simple ``Agent``-protocol object
(``async def run(text) -> str``) and exposes it through the bridge
``invoke()`` / ``apply_interruption()`` / ``reset()`` surface.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents._agent_runner import AgentRunner, AgentRunnerConfig
from easycat.integrations.agents._helpers import INTERRUPTION_NOTE
from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentTurnInput,
    CancellationMode,
    ExternalAgentBridge,
    RecorderContext,
)
from easycat.timeouts import AgentTimeoutError


def _recorder() -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=None,
        artifact_store=None,
        context=RecorderContext(
            run_id=f"run-{uuid4().hex[:8]}",
            session_id="test",
        ),
    )


async def _drain(runner: AgentRunner, text: str, cancel_token: CancelToken | None = None):
    events: list[AgentBridgeEvent] = []
    async for ev in runner.invoke(AgentTurnInput.from_text(text), _recorder(), cancel_token):
        events.append(ev)
    return events


# ── Test agents ────────────────────────────────────────────────────


class EchoAgent:
    async def run(self, text: str) -> str:
        return f"Echo: {text}"


class UpperAgent:
    async def run(self, text: str) -> str:
        return text.upper()


class FailingAgent:
    async def run(self, text: str) -> str:
        raise ValueError("agent broke")


class HangingAgent:
    async def run(self, text: str) -> str:
        await asyncio.sleep(999)
        return "never"


# ── Protocol conformance ──────────────────────────────────────────


def test_agent_runner_is_a_bridge():
    runner = AgentRunner(EchoAgent())
    assert isinstance(runner, ExternalAgentBridge)


# ── invoke() tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_yields_text_delta_and_done():
    runner = AgentRunner(EchoAgent())
    events = await _drain(runner, "hello")
    assert [e.kind for e in events] == ["text_delta", "done"]
    assert events[0].text == "Echo: hello"
    assert events[1].text == "Echo: hello"


@pytest.mark.asyncio
async def test_invoke_records_history():
    runner = AgentRunner(EchoAgent())
    await _drain(runner, "hello")
    assert runner.history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Echo: hello"},
    ]


@pytest.mark.asyncio
async def test_invoke_multi_turn_history():
    runner = AgentRunner(UpperAgent())
    await _drain(runner, "first")
    await _drain(runner, "second")
    assert len(runner.history) == 4


@pytest.mark.asyncio
async def test_reset_clears_history():
    runner = AgentRunner(EchoAgent())
    await _drain(runner, "hello")
    runner.reset()
    assert runner.history == []


@pytest.mark.asyncio
async def test_timeout_rolls_back_history():
    runner = AgentRunner(HangingAgent(), AgentRunnerConfig(timeout=0.05))
    with pytest.raises(AgentTimeoutError) as exc:
        await _drain(runner, "test")
    assert exc.value.timeout == 0.05
    assert runner.history == []


@pytest.mark.asyncio
async def test_agent_exception_rolls_back_history():
    runner = AgentRunner(FailingAgent())
    with pytest.raises(ValueError, match="agent broke"):
        await _drain(runner, "test")
    assert runner.history == []


@pytest.mark.asyncio
async def test_invoke_cancelled_before_completion_skips_events():
    token = CancelToken()

    class InstantAgent:
        async def run(self, text: str) -> str:
            return text

    runner = AgentRunner(InstantAgent())
    token.cancel()
    events = await _drain(runner, "hello", token)
    # Cancellation before completion yields no events; history still records
    # the assistant response so apply_interruption can truncate it.
    assert events == []
    assert runner.history[-1]["role"] == "assistant"


# ── apply_interruption / replace / append tests ───────────────────


@pytest.mark.asyncio
async def test_apply_interruption_truncates_last_assistant():
    runner = AgentRunner(EchoAgent())
    await _drain(runner, "hello")
    runner.apply_interruption("Echo: hel", CancellationMode.IMMEDIATE_STOP)
    assert runner.history[-1] == {"role": "assistant", "content": "Echo: hel..."}


@pytest.mark.asyncio
async def test_apply_interruption_empty_text_clears_assistant():
    # Parity with every real bridge: an interruption before any audio was
    # delivered rewrites the assistant message to "" (not a bare "...").
    runner = AgentRunner(EchoAgent())
    await _drain(runner, "hello")
    runner.apply_interruption("", CancellationMode.IMMEDIATE_STOP)
    assert runner.history[-1] == {"role": "assistant", "content": ""}


@pytest.mark.asyncio
async def test_replace_last_assistant_text_updates_history():
    runner = AgentRunner(EchoAgent())
    await _drain(runner, "hello")
    runner.replace_last_assistant_text("cleaned")
    assert runner.history[-1] == {"role": "assistant", "content": "cleaned"}


def test_replace_last_assistant_text_with_no_history_is_noop():
    runner = AgentRunner(EchoAgent())
    runner.replace_last_assistant_text("cleaned")
    assert runner.history == []


@pytest.mark.asyncio
async def test_append_interruption_note_adds_system_entry():
    runner = AgentRunner(EchoAgent())
    await _drain(runner, "hello")
    runner.append_interruption_note(INTERRUPTION_NOTE)
    assert runner.history[-1] == {"role": "system", "content": INTERRUPTION_NOTE}


@pytest.mark.asyncio
async def test_append_interruption_note_dedupes():
    runner = AgentRunner(EchoAgent())
    await _drain(runner, "hello")
    runner.append_interruption_note(INTERRUPTION_NOTE)
    runner.append_interruption_note(INTERRUPTION_NOTE)
    notes = [e for e in runner.history if e["role"] == "system"]
    assert len(notes) == 1


# ── Bridge delegation ─────────────────────────────────────────────


class _FakeBridge:
    COMMITTABLE_BOUNDARIES: dict = {}

    def __init__(self):
        self.invoke_called = False
        self.interruption_called = False
        self.reset_called = False
        self.replaced_text: str | None = None
        self.appended_note: str | None = None

    async def invoke(self, turn_input, recorder, cancel_token=None):
        self.invoke_called = True
        yield AgentBridgeEvent(kind="text_delta", text="bridged")
        yield AgentBridgeEvent(kind="done", text="bridged")

    def snapshot_state(self):
        from easycat.integrations.agents.base import FrameworkStateSnapshot

        return FrameworkStateSnapshot(fields={}, kind="fake")

    def apply_interruption(self, delivered_text, mode, recorder=None, caused_by_signal_id=None):
        self.interruption_called = True

    def replace_last_assistant_text(self, text):
        self.replaced_text = text

    def append_interruption_note(self, note):
        self.appended_note = note

    def reset(self):
        self.reset_called = True


@pytest.mark.asyncio
async def test_agent_runner_wrapping_a_bridge_delegates_invoke():
    inner = _FakeBridge()
    runner = AgentRunner(inner)
    assert runner.is_bridge
    events = await _drain(runner, "hello")
    assert inner.invoke_called
    assert [e.kind for e in events] == ["text_delta", "done"]


def test_agent_runner_wrapping_a_bridge_delegates_history_ops():
    inner = _FakeBridge()
    runner = AgentRunner(inner)
    runner.apply_interruption("spoken", CancellationMode.IMMEDIATE_STOP)
    runner.replace_last_assistant_text("clean")
    runner.append_interruption_note(INTERRUPTION_NOTE)
    runner.reset()
    assert inner.interruption_called
    assert inner.replaced_text == "clean"
    assert inner.appended_note == INTERRUPTION_NOTE
    assert inner.reset_called


class _HangingBridge:
    COMMITTABLE_BOUNDARIES: dict = {}

    async def invoke(self, turn_input, recorder, cancel_token=None):
        await asyncio.sleep(999)
        yield AgentBridgeEvent(kind="done", text="never")  # pragma: no cover

    def snapshot_state(self):
        from easycat.integrations.agents.base import FrameworkStateSnapshot

        return FrameworkStateSnapshot(fields={}, kind="hanging")

    def apply_interruption(self, delivered_text, mode, recorder=None, caused_by_signal_id=None):
        pass

    def replace_last_assistant_text(self, text):
        pass

    def append_interruption_note(self, note):
        pass

    def reset(self):
        pass


@pytest.mark.asyncio
async def test_bridge_delegation_honors_configured_timeout():
    runner = AgentRunner(_HangingBridge(), AgentRunnerConfig(timeout=0.05))
    with pytest.raises(AgentTimeoutError) as exc:
        await _drain(runner, "hello")
    assert exc.value.timeout == 0.05
    assert runner.history == []


class _SucceedThenHangBridge:
    """Replies normally on the first turn, then hangs forever."""

    COMMITTABLE_BOUNDARIES: dict = {}

    def __init__(self):
        self.turn = 0

    async def invoke(self, turn_input, recorder, cancel_token=None):
        self.turn += 1
        if self.turn == 1:
            yield AgentBridgeEvent(kind="text_delta", text="ok")
            yield AgentBridgeEvent(kind="done", text="ok")
            return
        await asyncio.sleep(999)
        yield AgentBridgeEvent(kind="done", text="never")  # pragma: no cover

    def snapshot_state(self):
        from easycat.integrations.agents.base import FrameworkStateSnapshot

        return FrameworkStateSnapshot(fields={}, kind="succeed-then-hang")

    def apply_interruption(self, delivered_text, mode, recorder=None, caused_by_signal_id=None):
        pass

    def replace_last_assistant_text(self, text):
        pass

    def append_interruption_note(self, note):
        pass

    def reset(self):
        pass


@pytest.mark.asyncio
async def test_bridge_timeout_leaves_no_dangling_user_entry():
    # Regression: a timed-out bridge turn must not record a user message into
    # the runner's advisory shadow history, since the inner bridge owns the
    # authoritative (partial) turn state and cannot be rolled back.
    runner = AgentRunner(_SucceedThenHangBridge(), AgentRunnerConfig(timeout=0.05))
    await _drain(runner, "first")
    assert runner.history == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
    ]
    with pytest.raises(AgentTimeoutError):
        await _drain(runner, "second")
    # The timed-out turn left the shadow history untouched (no orphan user
    # entry) so the next turn won't double-feed context.
    assert runner.history == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
    ]


class _ContextCapturingBridge:
    COMMITTABLE_BOUNDARIES: dict = {}

    def __init__(self):
        self.seen_contexts: list[list[dict[str, str]]] = []

    async def invoke(self, turn_input, recorder, cancel_token=None):
        self.seen_contexts.append(list(turn_input.context))
        yield AgentBridgeEvent(kind="text_delta", text=f"reply-{len(self.seen_contexts)}")
        yield AgentBridgeEvent(kind="done", text=f"reply-{len(self.seen_contexts)}")

    def snapshot_state(self):
        from easycat.integrations.agents.base import FrameworkStateSnapshot

        return FrameworkStateSnapshot(fields={}, kind="ctx")

    def apply_interruption(self, delivered_text, mode, recorder=None, caused_by_signal_id=None):
        pass

    def replace_last_assistant_text(self, text):
        pass

    def append_interruption_note(self, note):
        pass

    def reset(self):
        pass


@pytest.mark.asyncio
async def test_bridge_delegation_forwards_runner_history_as_context():
    inner = _ContextCapturingBridge()
    runner = AgentRunner(inner)
    await _drain(runner, "first")
    await _drain(runner, "second")
    # First turn: no prior history -> empty context.
    assert inner.seen_contexts[0] == []
    # Second turn: prior user+assistant from turn 1 flow through as context.
    assert inner.seen_contexts[1] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply-1"},
    ]
