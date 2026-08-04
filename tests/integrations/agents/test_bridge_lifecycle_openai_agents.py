"""WS3.1 lifecycle driver for the OpenAI Agents bridge."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents import openai_agents as openai_agents_module
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentTurnInput,
    CancellationMode,
    FrameworkStateSnapshot,
)
from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge
from easycat.testing import RecordingAgentRecorder
from easycat.testing._bridge_lifecycle import (
    BridgeLifecycleScenarioSuite,
    HistoryIsolationObservation,
    NormalizedHistoryEntry,
    NormalizedLifecycleState,
    RecorderCleanupObservation,
    StreamCloseObservation,
    ToolCancellationObservation,
    UnknownEventObservation,
)

pytestmark = [
    pytest.mark.agent_bridge,
    pytest.mark.surface_agent,
    pytest.mark.provider("openai-agents-lifecycle-driver"),
]


@pytest.fixture(autouse=True)
def _restore_sdk_runner() -> Iterator[None]:
    original = openai_agents_module.Runner
    yield
    openai_agents_module.Runner = original


class _ControlledSDKEvents:
    def __init__(self, items: list[object]) -> None:
        self._items = items
        self._index = 0
        self.started = False
        self.exhausted = False
        self.closed = False
        self.waiting = asyncio.Event()
        self.close_calls = 0
        self.running_work_cancelled = False

    def __aiter__(self) -> _ControlledSDKEvents:
        return self

    async def __anext__(self) -> Any:
        self.started = True
        while not self.closed and self._index < len(self._items):
            item = self._items[self._index]
            self._index += 1
            if isinstance(item, asyncio.Event):
                self.waiting.set()
                await item.wait()
                continue
            return item
        if not self.closed:
            self.exhausted = True
        raise StopAsyncIteration

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_calls += 1
        self.running_work_cancelled = not self.exhausted


class _Agent:
    def __init__(self) -> None:
        self.name = "LifecycleAgent"
        self.model = "gpt-lifecycle"
        self.mcp_servers: list[Any] = []


class _ControlledRunResult:
    def __init__(
        self,
        *,
        agent: _Agent,
        source: _ControlledSDKEvents,
        history: list[dict[str, Any]],
        response_id: str,
    ) -> None:
        self.last_agent = agent
        self.last_response_id = response_id
        self.final_output = None
        self.context_wrapper = SimpleNamespace(usage=None)
        self.source = source
        self.history = history
        self.cancel_calls: list[str] = []

    def stream_events(self) -> AsyncIterator[Any]:
        return self.source

    def cancel(self, mode: str = "immediate") -> None:
        self.cancel_calls.append(mode)

    def to_input_list(self) -> list[dict[str, Any]]:
        return list(self.history)


class _ControlledRunner:
    def __init__(self) -> None:
        self.result: _ControlledRunResult | None = None

    def run_streamed(self, agent: Any, input_data: Any, **kwargs: Any) -> _ControlledRunResult:
        del agent, input_data, kwargs
        assert self.result is not None
        return self.result


def _text_event(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="raw_response_event",
        data=SimpleNamespace(type="response.output_text.delta", delta=text),
    )


def _tool_start() -> SimpleNamespace:
    return SimpleNamespace(
        type="run_item_stream_event",
        item=SimpleNamespace(
            type="tool_call_item",
            raw_item=SimpleNamespace(name="lookup", call_id="call-1"),
        ),
    )


def _tool_result() -> SimpleNamespace:
    return SimpleNamespace(
        type="run_item_stream_event",
        item=SimpleNamespace(
            type="tool_call_output_item",
            raw_item={
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "ok",
            },
            output="ok",
        ),
    )


def _unknown_item() -> SimpleNamespace:
    return SimpleNamespace(
        type="run_item_stream_event",
        item=SimpleNamespace(type="future_run_item"),
    )


def _recorder_cursor_ids(
    recorder: RecordingAgentRecorder,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    entered = tuple(
        record[1][0].unit_id for record in recorder.records if record[0] == "unit_entered"
    )
    exited = tuple(
        record[1][0].unit_id for record in recorder.records if record[0] == "unit_exited"
    )
    return entered, exited


class _OpenAIAgentsLifecycleDriver:
    def __init__(self) -> None:
        self._original_runner = openai_agents_module.Runner
        self.runner = _ControlledRunner()
        openai_agents_module.Runner = self.runner
        self.agent = _Agent()
        self.bridge = OpenAIAgentsBridge(self.agent)
        self.source: _ControlledSDKEvents | None = None
        self.result: _ControlledRunResult | None = None

    def _configure(
        self,
        items: list[object],
        *,
        history: list[dict[str, Any]],
        response_id: str,
    ) -> _ControlledSDKEvents:
        source = _ControlledSDKEvents(items)
        result = _ControlledRunResult(
            agent=self.agent,
            source=source,
            history=history,
            response_id=response_id,
        )
        self.source = source
        self.result = result
        self.runner.result = result
        return source

    async def observe_unknown_event_tolerance(self, *, valid_text: str) -> UnknownEventObservation:
        self._configure(
            [_unknown_item(), _text_event(valid_text)],
            history=[
                {"role": "user", "content": "unknown event"},
                {"role": "assistant", "content": valid_text},
            ],
            response_id="resp-unknown",
        )
        events = tuple(
            [
                event
                async for event in self.bridge.invoke(
                    AgentTurnInput.from_text("unknown event"), RecordingAgentRecorder()
                )
            ]
        )
        return UnknownEventObservation(events=events)

    async def observe_tool_inflight_cancellation(
        self, *, delivered_text: str
    ) -> ToolCancellationObservation:
        gate = asyncio.Event()
        source = self._configure(
            [
                _text_event(delivered_text),
                _tool_start(),
                gate,
                _tool_result(),
                _text_event("must not be delivered"),
            ],
            history=[
                {"role": "user", "content": "use a tool"},
                {"role": "assistant", "content": delivered_text},
            ],
            response_id="resp-tool",
        )
        token = CancelToken()
        recorder = RecordingAgentRecorder()
        stream = self.bridge.invoke(AgentTurnInput.from_text("use a tool"), recorder, token)
        before_cancel: list[AgentBridgeEvent] = []
        while True:
            event = await stream.__anext__()
            before_cancel.append(event)
            if event.kind == "tool_started":
                break
        phases_before_cancel = tuple(recorder.tool_phases())
        pending_result = asyncio.create_task(stream.__anext__())
        await source.waiting.wait()
        token.cancel()
        gate.set()
        after_cancel = [await pending_result]
        after_cancel.extend([event async for event in stream])
        phases = tuple(recorder.tool_phases())
        committed_assistant_text = self._history()[-1].text
        self.bridge.apply_interruption(
            delivered_text,
            CancellationMode.DRAIN_CURRENT_UNIT,
            recorder=recorder,
        )

        return ToolCancellationObservation(
            events_before_cancel=tuple(before_cancel),
            events_after_cancel=tuple(after_cancel),
            tool_phases_before_cancel=phases_before_cancel,
            tool_phases_after_cancel=phases[len(phases_before_cancel) :],
            committed_assistant_text=committed_assistant_text,
            inner_stream_close_calls=source.close_calls,
        )

    async def observe_stream_close_cleanup(self) -> StreamCloseObservation:
        gate = asyncio.Event()
        source = self._configure(
            [_text_event("partial"), gate, _text_event("must not be delivered")],
            history=[
                {"role": "user", "content": "close"},
                {"role": "assistant", "content": "partial"},
            ],
            response_id="resp-close",
        )
        stream = self.bridge.invoke(AgentTurnInput.from_text("close"), RecordingAgentRecorder())
        await stream.__anext__()
        await stream.aclose()
        assert self.result is not None
        assert self.result.cancel_calls == ["immediate"]
        return StreamCloseObservation(
            inner_stream_close_calls=source.close_calls,
            running_work_cancelled=source.running_work_cancelled,
        )

    async def observe_recorder_transient_cleanup(self) -> RecorderCleanupObservation:
        gate = asyncio.Event()
        source = self._configure(
            [_tool_start(), gate, _tool_result()],
            history=[{"role": "user", "content": "cleanup"}],
            response_id="resp-cleanup",
        )
        recorder = RecordingAgentRecorder()
        stream = self.bridge.invoke(AgentTurnInput.from_text("cleanup"), recorder)
        await stream.__anext__()
        await stream.aclose()
        entered, exited = _recorder_cursor_ids(recorder)
        return RecorderCleanupObservation(
            entered_cursor_ids=entered,
            exited_cursor_ids=exited,
            transient_items_after_close=self._transient_items(),
            inner_stream_close_calls=source.close_calls,
        )

    async def observe_interruption_history_isolation(
        self, *, prior_user_text: str, prior_assistant_text: str
    ) -> HistoryIsolationObservation:
        prior = [
            {"role": "user", "content": prior_user_text},
            {"role": "assistant", "content": prior_assistant_text},
        ]
        self.bridge._message_history = list(prior)
        before = self._project_prior_history()
        gate = asyncio.Event()
        source = self._configure(
            [gate, _text_event("must not be delivered")],
            history=[*prior, {"role": "user", "content": "new turn"}],
            response_id="resp-current",
        )
        recorder = RecordingAgentRecorder()
        stream = self.bridge.invoke(AgentTurnInput.from_text("new turn"), recorder)
        pending_event = asyncio.create_task(stream.__anext__())
        await source.waiting.wait()
        pending_event.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending_event
        await stream.aclose()
        self.bridge.apply_interruption(
            "",
            CancellationMode.IMMEDIATE_STOP,
            recorder=recorder,
        )
        return HistoryIsolationObservation(
            prior_history_before=before,
            prior_history_after=self._project_prior_history(),
        )

    def _history(self) -> tuple[NormalizedHistoryEntry, ...]:
        return tuple(
            NormalizedHistoryEntry(
                role=str(item.get("role", "")),
                text=str(item.get("content", "")),
            )
            for item in self.bridge._message_history
            if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
        )

    def _project_prior_history(self) -> tuple[NormalizedHistoryEntry, ...]:
        return self._history()[:2]

    def _transient_items(self) -> int:
        active_source = int(bool(self.source and self.source.started and not self.source.closed))
        return active_source + int(self.bridge._pending_interruption is not None)

    def normalized_state(self) -> NormalizedLifecycleState:
        active_streams = int(bool(self.source and self.source.started and not self.source.closed))
        return NormalizedLifecycleState(
            history=self._history(),
            active_streams=active_streams,
            transient_items=self._transient_items(),
        )

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return self.bridge.snapshot_state()

    def reset(self) -> None:
        self.bridge.reset()
        openai_agents_module.Runner = self._original_runner
        self.runner.result = None
        self.source = None
        self.result = None


class TestOpenAIAgentsBridgeLifecycleScenarios(BridgeLifecycleScenarioSuite):
    driver_factory = _OpenAIAgentsLifecycleDriver


def test_openai_agents_suite_scenarios_match_execution_matrix() -> None:
    matrix_path = Path(__file__).with_name("bridge-lifecycle-matrix.json")
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    execution = matrix["driver_execution"]["bridges"]["openai_agents"]

    assert execution["status"] == "wired"
    assert set(execution["scenarios"]) == (
        TestOpenAIAgentsBridgeLifecycleScenarios.applicable_scenarios
    )
