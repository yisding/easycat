"""Tests for AgentRunner as an ExternalAgentBridge.

``AgentRunner`` wraps a simple ``Agent``-protocol object
(``async def run(text) -> str``) and exposes it through the bridge
``invoke()`` / ``apply_interruption()`` / ``reset()`` surface.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock
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
    NullAgentRecorder,
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


@pytest.mark.parametrize("timeout", [True, float("nan"), float("inf"), float("-inf")])
def test_config_rejects_nonfinite_timeout(timeout: object) -> None:
    with pytest.raises(ValueError, match="timeout must be a finite number or None"):
        AgentRunnerConfig(timeout=timeout)


@pytest.mark.parametrize("retries", [True, "3", 1.5, None])
def test_config_rejects_noninteger_preemptive_retry_limit(retries: object) -> None:
    with pytest.raises(ValueError, match="preemptive_max_retries must be an integer"):
        AgentRunnerConfig(preemptive_max_retries=retries)


@pytest.mark.parametrize("enabled", [0, 1, "true", None])
def test_config_rejects_nonboolean_preemptive_generation(enabled: object) -> None:
    with pytest.raises(ValueError, match="preemptive_generation must be a boolean"):
        AgentRunnerConfig(preemptive_generation=enabled)


@pytest.mark.parametrize("retries", [0, -1])
def test_config_rejects_nonpositive_preemptive_retry_limit(retries: int) -> None:
    with pytest.raises(ValueError, match="preemptive_max_retries must be >= 1"):
        AgentRunnerConfig(preemptive_max_retries=retries)


class UpperAgent:
    async def run(self, text: str) -> str:
        return text.upper()


class FailingAgent:
    async def run(self, text: str) -> str:
        raise ValueError("agent broke")


class HangingAgent:
    async def run(self, text: str) -> str:
        await asyncio.Event().wait()
        return "never"


def _preemptive_runner(agent: object) -> AgentRunner:
    return AgentRunner(agent, AgentRunnerConfig(preemptive_generation=True))


# ── Protocol conformance ──────────────────────────────────────────


def test_agent_runner_is_a_bridge():
    runner = AgentRunner(EchoAgent())
    assert isinstance(runner, ExternalAgentBridge)


@pytest.mark.asyncio
async def test_history_returns_independent_message_dicts():
    runner = AgentRunner(EchoAgent())
    await _drain(runner, "hello")

    history = runner.history
    history[0]["content"] = "corrupted"

    assert runner.history[0] == {"role": "user", "content": "hello"}


# ── invoke() tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_yields_text_delta_and_done():
    runner = AgentRunner(EchoAgent())
    events = await _drain(runner, "hello")
    assert [e.kind for e in events] == ["text_delta", "done"]
    assert events[0].text == "Echo: hello"
    assert events[1].text == "Echo: hello"


@pytest.mark.asyncio
async def test_plain_agent_rejects_nonstring_response_without_committing_history() -> None:
    class InvalidAgent:
        async def run(self, text: str) -> object:
            return {"text": text}

    runner = AgentRunner(InvalidAgent())

    with pytest.raises(TypeError, match="plain agent response must be str"):
        await _drain(runner, "hello")

    assert runner.history == []


@pytest.mark.asyncio
async def test_plain_agent_rejects_system_turn_input():
    runner = AgentRunner(EchoAgent())

    with pytest.raises(ValueError, match="system application prompts require"):
        async for _ in runner.invoke(
            AgentTurnInput.from_text("instruction", role="system"),
            NullAgentRecorder(),
        ):
            pass

    assert runner.history == []


@pytest.mark.asyncio
async def test_plain_agent_accepts_transient_context_for_voice_compatibility():
    runner = AgentRunner(EchoAgent())
    turn_input = AgentTurnInput.from_text(
        "hello",
        context=[{"role": "system", "content": "Caller: +15551234567"}],
    )

    events = [event async for event in runner.invoke(turn_input, NullAgentRecorder())]

    assert [event.kind for event in events] == ["text_delta", "done"]
    assert runner.history[0] == {"role": "user", "content": "hello"}


@pytest.mark.asyncio
async def test_null_recorder_skips_cursor_metadata(monkeypatch):
    runner = AgentRunner(EchoAgent())
    monkeypatch.setattr(
        "easycat.integrations.agents._agent_runner.uuid4",
        lambda: pytest.fail("cursor ids should not be built without recording"),
    )

    events = [
        event
        async for event in runner.invoke(AgentTurnInput.from_text("hello"), NullAgentRecorder())
    ]

    assert [event.kind for event in events] == ["text_delta", "done"]


@pytest.mark.asyncio
async def test_null_recorder_preserves_agent_failure():
    runner = AgentRunner(FailingAgent())

    with pytest.raises(ValueError, match="agent broke"):
        async for _ in runner.invoke(AgentTurnInput.from_text("hello"), NullAgentRecorder()):
            pass

    assert runner.history == []


@pytest.mark.asyncio
async def test_invoke_records_history():
    runner = AgentRunner(EchoAgent())
    await _drain(runner, "hello")
    assert runner.history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Echo: hello"},
    ]


@pytest.mark.asyncio
async def test_commit_guard_rolls_back_simple_agent_history_after_provider_wait():
    class BlockingAgent:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self, text: str) -> str:
            _ = text
            self.started.set()
            await self.release.wait()
            return "stale"

    agent = BlockingAgent()
    runner = AgentRunner(agent)
    current = True

    async def _invoke() -> list[AgentBridgeEvent]:
        return [
            event
            async for event in runner.invoke(
                AgentTurnInput.from_text("hello"),
                NullAgentRecorder(),
                commit_guard=lambda: current,
            )
        ]

    invocation = asyncio.create_task(_invoke())
    await agent.started.wait()
    current = False
    agent.release.set()

    assert await invocation == []
    assert runner.history == []


@pytest.mark.asyncio
async def test_prepare_response_defers_history_until_invoke_prepared():
    runner = _preemptive_runner(EchoAgent())

    prepared = await runner.prepare_response(AgentTurnInput.from_text("hello"))

    assert prepared.response == "Echo: hello"
    assert runner.history == []

    events = [event async for event in runner.invoke_prepared(prepared, _recorder())]
    assert [event.kind for event in events] == ["text_delta", "done"]
    assert runner.history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Echo: hello"},
    ]


@pytest.mark.asyncio
async def test_prepare_response_rejects_nonstring_agent_output() -> None:
    class InvalidAgent:
        async def run(self, _text: str) -> object:
            return ["not", "text"]

    runner = _preemptive_runner(InvalidAgent())

    with pytest.raises(TypeError, match="plain agent response must be str"):
        await runner.prepare_response(AgentTurnInput.from_text("hello"))

    assert runner.history == []


@pytest.mark.asyncio
async def test_plain_agent_preparation_rejects_system_input():
    runner = _preemptive_runner(EchoAgent())

    with pytest.raises(ValueError, match="system application prompts require"):
        await runner.prepare_response(AgentTurnInput.from_text("instruction", role="system"))

    assert runner.history == []


@pytest.mark.asyncio
async def test_discarded_prepared_response_never_mutates_history():
    runner = _preemptive_runner(EchoAgent())

    await runner.prepare_response(AgentTurnInput.from_text("discard me"))

    assert runner.history == []


@pytest.mark.asyncio
async def test_cancelled_prepared_response_never_commits_history():
    runner = _preemptive_runner(EchoAgent())
    prepared = await runner.prepare_response(AgentTurnInput.from_text("cancel me"))
    token = CancelToken()
    token.cancel()

    events = [event async for event in runner.invoke_prepared(prepared, _recorder(), token)]

    assert events == []
    assert runner.history == []
    assert not prepared.committed


@pytest.mark.asyncio
async def test_stale_prepared_response_never_commits_history():
    runner = _preemptive_runner(EchoAgent())
    prepared = await runner.prepare_response(AgentTurnInput.from_text("stale"))

    events = [
        event
        async for event in runner.invoke_prepared(
            prepared,
            _recorder(),
            commit_guard=lambda: False,
        )
    ]

    assert events == []
    assert runner.history == []
    assert not prepared.committed


@pytest.mark.asyncio
async def test_preemptive_generation_is_opt_in():
    runner = AgentRunner(EchoAgent())

    assert not runner.supports_preemptive_generation
    with pytest.raises(RuntimeError, match="does not support"):
        await runner.prepare_response(AgentTurnInput.from_text("hello"))

    assert _preemptive_runner(EchoAgent()).supports_preemptive_generation


@pytest.mark.asyncio
async def test_prepared_cursor_uses_actual_generation_start_time():
    runner = _preemptive_runner(EchoAgent())
    before = time.monotonic_ns()
    prepared = await runner.prepare_response(AgentTurnInput.from_text("hello"))
    recorder = MagicMock()

    events = [event async for event in runner.invoke_prepared(prepared, recorder)]

    cursor = recorder.record_unit_entered.call_args.args[0]
    assert prepared.started_at_ns >= before
    assert cursor.entered_at == prepared.started_at_ns
    assert [event.kind for event in events] == ["text_delta", "done"]


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
async def test_zero_timeout_rejects_immediate_plain_agent():
    runner = AgentRunner(EchoAgent(), AgentRunnerConfig(timeout=0.0))

    with pytest.raises(AgentTimeoutError) as exc:
        await _drain(runner, "test")

    assert exc.value.timeout == 0.0
    assert runner.history == []


@pytest.mark.asyncio
async def test_configured_timeout_keeps_plain_agent_in_caller_task():
    caller_task = asyncio.current_task()

    class TaskCapturingAgent:
        task: asyncio.Task[object] | None = None

        async def run(self, text: str) -> str:
            _ = text
            self.task = asyncio.current_task()
            return "ok"

    agent = TaskCapturingAgent()
    runner = AgentRunner(agent, AgentRunnerConfig(timeout=1.0))

    await _drain(runner, "hello")

    assert agent.task is caller_task


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
    # Work that is cancelled before invocation never starts or mutates history.
    assert events == []
    assert runner.history == []


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
    COMMITTABLE_BOUNDARIES: dict = {}  # noqa: RUF012 test fake uses shared class fixture

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


class _CancelAwareBridge(_FakeBridge):
    async def invoke(self, turn_input, recorder, cancel_token=None):
        self.invoke_called = True
        if cancel_token and cancel_token.is_cancelled:
            return
        yield AgentBridgeEvent(kind="done", text="bridged")


class _CancellationIgnoringBridge(_FakeBridge):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, turn_input, recorder, cancel_token=None):
        _ = turn_input, recorder, cancel_token
        self.invoke_called = True
        self.started.set()
        await self.release.wait()
        yield AgentBridgeEvent(kind="text_delta", text="stale")
        yield AgentBridgeEvent(kind="done", text="stale")


class _CancellationIgnoringToolBridge(_FakeBridge):
    def __init__(self):
        super().__init__()
        self.tool_started = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, turn_input, recorder, cancel_token=None):
        _ = turn_input, recorder, cancel_token
        self.invoke_called = True
        yield AgentBridgeEvent(kind="tool_started", tool_name="write", call_id="call-1")
        self.tool_started.set()
        await self.release.wait()
        yield AgentBridgeEvent(kind="tool_started", tool_name="late", call_id="call-2")
        yield AgentBridgeEvent(kind="tool_delta", call_id="call-2", text="stale")
        yield AgentBridgeEvent(kind="tool_result", call_id="call-2", result="stale")
        yield AgentBridgeEvent(kind="tool_delta", call_id="call-1", text="finishing")
        yield AgentBridgeEvent(kind="tool_result", call_id="call-1", result="written")
        yield AgentBridgeEvent(kind="text_delta", text="stale")
        yield AgentBridgeEvent(kind="done", text="stale")


class _RepeatedToolIdBridge(_FakeBridge):
    def __init__(self):
        super().__init__()
        self.tools_started = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, turn_input, recorder, cancel_token=None):
        _ = turn_input, recorder, cancel_token
        yield AgentBridgeEvent(kind="tool_started", tool_name="first")
        yield AgentBridgeEvent(kind="tool_started", tool_name="second")
        self.tools_started.set()
        await self.release.wait()
        yield AgentBridgeEvent(kind="tool_result", result="first done")
        yield AgentBridgeEvent(kind="tool_result", result="second done")
        yield AgentBridgeEvent(kind="text_delta", text="stale")


@pytest.mark.asyncio
async def test_agent_runner_wrapping_a_bridge_delegates_invoke():
    inner = _FakeBridge()
    runner = AgentRunner(inner)
    assert runner.is_bridge
    events = await _drain(runner, "hello")
    assert inner.invoke_called
    assert [e.kind for e in events] == ["text_delta", "done"]


@pytest.mark.asyncio
async def test_wrapped_bridge_skips_already_cancelled_turn():
    inner = _CancelAwareBridge()
    runner = AgentRunner(inner)
    token = CancelToken()
    token.cancel()

    events = await _drain(runner, "hello", token)

    assert events == []
    assert inner.invoke_called is False
    assert runner.history == []


@pytest.mark.asyncio
async def test_wrapped_bridge_drops_output_after_cancellation():
    inner = _CancellationIgnoringBridge()
    runner = AgentRunner(inner)
    token = CancelToken()
    task = asyncio.create_task(_drain(runner, "old", token))

    await asyncio.wait_for(inner.started.wait(), timeout=1)
    token.cancel()
    inner.release.set()

    assert await asyncio.wait_for(task, timeout=1) == []
    assert runner.history == []


@pytest.mark.asyncio
async def test_wrapped_bridge_drains_inflight_tool_result_after_cancellation():
    inner = _CancellationIgnoringToolBridge()
    runner = AgentRunner(inner)
    token = CancelToken()
    task = asyncio.create_task(_drain(runner, "old", token))

    await asyncio.wait_for(inner.tool_started.wait(), timeout=1)
    token.cancel()
    inner.release.set()

    events = await asyncio.wait_for(task, timeout=1)
    assert [(event.kind, event.call_id) for event in events] == [
        ("tool_started", "call-1"),
        ("tool_delta", "call-1"),
        ("tool_result", "call-1"),
    ]
    assert runner.history == []


@pytest.mark.asyncio
async def test_wrapped_bridge_preserves_pending_tool_multiplicity_after_cancellation():
    inner = _RepeatedToolIdBridge()
    runner = AgentRunner(inner)
    token = CancelToken()
    task = asyncio.create_task(_drain(runner, "old", token))

    await asyncio.wait_for(inner.tools_started.wait(), timeout=1)
    token.cancel()
    inner.release.set()

    events = await asyncio.wait_for(task, timeout=1)
    assert [event.kind for event in events] == [
        "tool_started",
        "tool_started",
        "tool_result",
        "tool_result",
    ]
    assert runner.history == []


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


def test_agent_runner_is_warmupable():
    """The default wrapper must be warmupable so the inner bridge can warm.

    Without a ``warmup`` method, ``warmupable(AgentRunner)`` is ``None`` and
    the warmup loop skips the agent entirely.
    """
    from easycat.runtime.capabilities import warmupable

    runner = AgentRunner(_FakeBridge())
    assert warmupable(runner) is not None


@pytest.mark.asyncio
async def test_agent_runner_warmup_delegates_to_inner_warmup():
    class _WarmupBridge(_FakeBridge):
        def __init__(self):
            super().__init__()
            self.warmed = False

        async def warmup(self):
            self.warmed = True

    inner = _WarmupBridge()
    runner = AgentRunner(inner)
    await runner.warmup()
    assert inner.warmed is True


@pytest.mark.asyncio
async def test_agent_runner_warmup_noops_without_inner_warmup():
    """An agent with no ``warmup`` hook makes the runner's warmup a clean no-op."""

    class _PlainAgent:
        async def run(self, text: str) -> str:
            return text

    runner = AgentRunner(_PlainAgent())
    # Returns cleanly; nothing to forward to.
    await runner.warmup()


@pytest.mark.asyncio
async def test_agent_runner_rollback_warmup_delegates_to_inner_bridge() -> None:
    class _RollbackBridge(_FakeBridge):
        def __init__(self) -> None:
            super().__init__()
            self.rolled_back = False

        async def rollback_warmup(self) -> None:
            self.rolled_back = True

    inner = _RollbackBridge()
    runner = AgentRunner(inner)

    await runner.rollback_warmup()

    assert inner.rolled_back is True


class _PostDoneHangingBridge:
    COMMITTABLE_BOUNDARIES: dict = {}  # noqa: RUF012 test fake uses shared class fixture

    def __init__(self):
        self.closed = False

    async def invoke(self, turn_input, recorder, cancel_token=None):
        try:
            yield AgentBridgeEvent(kind="text_delta", text="ok")
            yield AgentBridgeEvent(kind="done", text="ok")
            await asyncio.Event().wait()
            yield AgentBridgeEvent(kind="text_delta", text="late")  # pragma: no cover
        finally:
            self.closed = True

    def snapshot_state(self):
        from easycat.integrations.agents.base import FrameworkStateSnapshot

        return FrameworkStateSnapshot(fields={}, kind="post-done-hanging")

    def apply_interruption(self, delivered_text, mode, recorder=None, caused_by_signal_id=None):
        pass

    def replace_last_assistant_text(self, text):
        pass

    def append_interruption_note(self, note):
        pass

    def reset(self):
        pass


@pytest.mark.asyncio
async def test_bridge_delegation_stops_after_done_without_timeout():
    inner = _PostDoneHangingBridge()
    runner = AgentRunner(inner, AgentRunnerConfig(timeout=None))

    events = await asyncio.wait_for(_drain(runner, "hello"), timeout=0.2)

    assert [event.kind for event in events] == ["text_delta", "done"]
    assert inner.closed
    assert runner.history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "ok"},
    ]


class _PrematureEOFBridge(_PostDoneHangingBridge):
    async def invoke(self, turn_input, recorder, cancel_token=None):
        try:
            yield AgentBridgeEvent(kind="text_delta", text="partial")
        finally:
            self.closed = True


@pytest.mark.asyncio
async def test_bridge_delegation_rejects_eof_before_terminal_done():
    inner = _PrematureEOFBridge()
    runner = AgentRunner(inner, AgentRunnerConfig(timeout=None))

    with pytest.raises(RuntimeError, match="before a terminal done"):
        await _drain(runner, "hello")

    assert inner.closed
    assert runner.history == []


@pytest.mark.asyncio
async def test_bridge_delegation_closes_inner_stream_on_early_consumer_close():
    inner = _PostDoneHangingBridge()
    runner = AgentRunner(inner, AgentRunnerConfig(timeout=None))
    stream = runner.invoke(AgentTurnInput.from_text("hello"), _recorder())

    first = await stream.__anext__()
    assert first == AgentBridgeEvent(kind="text_delta", text="ok")

    await stream.aclose()

    assert inner.closed
    assert runner.history == []


class _HangingBridge:
    COMMITTABLE_BOUNDARIES: dict = {}  # noqa: RUF012 test fake uses shared class fixture

    async def invoke(self, turn_input, recorder, cancel_token=None):
        await asyncio.Event().wait()
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


@pytest.mark.asyncio
async def test_configured_timeout_keeps_bridge_iteration_in_caller_task():
    caller_task = asyncio.current_task()

    class TaskCapturingBridge(_FakeBridge):
        task: asyncio.Task[object] | None = None

        async def invoke(self, turn_input, recorder, cancel_token=None):
            _ = turn_input, recorder, cancel_token
            self.task = asyncio.current_task()
            yield AgentBridgeEvent(kind="done", text="ok")

    bridge = TaskCapturingBridge()
    runner = AgentRunner(bridge, AgentRunnerConfig(timeout=1.0))

    await _drain(runner, "hello")

    assert bridge.task is caller_task


class _SucceedThenHangBridge:
    """Replies normally on the first turn, then hangs forever."""

    COMMITTABLE_BOUNDARIES: dict = {}  # noqa: RUF012 test fake uses shared class fixture

    def __init__(self):
        self.turn = 0

    async def invoke(self, turn_input, recorder, cancel_token=None):
        self.turn += 1
        if self.turn == 1:
            yield AgentBridgeEvent(kind="text_delta", text="ok")
            yield AgentBridgeEvent(kind="done", text="ok")
            return
        await asyncio.Event().wait()
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
    COMMITTABLE_BOUNDARIES: dict = {}  # noqa: RUF012 test fake uses shared class fixture

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


@pytest.mark.asyncio
async def test_bridge_cannot_mutate_runner_history_through_forwarded_context() -> None:
    class MutatingContextBridge(_ContextCapturingBridge):
        async def invoke(self, turn_input, recorder, cancel_token=None):
            if turn_input.context:
                turn_input.context[0]["content"] = "corrupted"
            async for event in super().invoke(turn_input, recorder, cancel_token):
                yield event

    inner = MutatingContextBridge()
    runner = AgentRunner(inner)

    await _drain(runner, "first")
    await _drain(runner, "second")

    assert runner.history[0] == {"role": "user", "content": "first"}


@pytest.mark.asyncio
async def test_bridge_nonstring_event_text_does_not_commit_shadow_history() -> None:
    class InvalidTextBridge(_FakeBridge):
        async def invoke(self, turn_input, recorder, cancel_token=None):
            yield AgentBridgeEvent(kind="done", text=["not", "text"])  # type: ignore[arg-type]

    runner = AgentRunner(InvalidTextBridge())

    with pytest.raises(TypeError, match="agent bridge done event text must be str"):
        await _drain(runner, "hello")

    assert runner.history == []


@pytest.mark.asyncio
async def test_bridge_text_event_without_text_is_rejected() -> None:
    class MissingTextBridge(_FakeBridge):
        async def invoke(self, turn_input, recorder, cancel_token=None):
            yield SimpleNamespace(kind="done")

    runner = AgentRunner(MissingTextBridge())

    with pytest.raises(TypeError, match="agent bridge done event text must be str"):
        await _drain(runner, "hello")

    assert runner.history == []


@pytest.mark.asyncio
async def test_bridge_tool_event_with_none_text_passes_through() -> None:
    """Duck-typed tool events legitimately carry ``text=None``."""

    class NoneTextToolBridge(_FakeBridge):
        async def invoke(self, turn_input, recorder, cancel_token=None):
            yield SimpleNamespace(
                kind="tool_started",
                text=None,
                tool_name="lookup",
                call_id="call-1",
            )
            yield AgentBridgeEvent(kind="done", text="answer")

    runner = AgentRunner(NoneTextToolBridge())

    events = [event async for event in runner.invoke(AgentTurnInput.from_text("hi"), _recorder())]

    assert [getattr(event, "kind", None) for event in events] == ["tool_started", "done"]
    assert events[-1].text == "answer"


@pytest.mark.asyncio
async def test_bridge_owned_history_capability_suppresses_runner_shadow_context():
    class HistoryOwningBridge(_ContextCapturingBridge):
        MANAGES_CONVERSATION_HISTORY = True

    inner = HistoryOwningBridge()
    runner = AgentRunner(inner)

    await _drain(runner, "first")
    await _drain(runner, "second")

    assert inner.seen_contexts == [[], []]
    assert runner.history == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply-1"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "reply-2"},
    ]
