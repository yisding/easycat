"""WS3.1 lifecycle drivers for the LangChain event-stream bridge family."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentTurnInput,
    CancellationMode,
    FrameworkStateSnapshot,
)
from easycat.integrations.agents.langchain import LangChainBridge
from easycat.integrations.agents.langgraph import LangGraphBridge
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

from ._langchain_bridge_support import _MockAIMessageChunk
from ._langgraph_bridge_support import _MockCompiledGraph, _MockMessage, _MockState

pytestmark = [
    pytest.mark.agent_bridge,
    pytest.mark.surface_agent,
    pytest.mark.provider("langchain-family-lifecycle-driver"),
]

_Family = Literal["langchain", "langgraph"]


class _ControlledEvents:
    """Close-aware framework iterator with an exact in-flight work probe."""

    def __init__(self, items: list[object]) -> None:
        self._items = items
        self._index = 0
        self.started = False
        self.exhausted = False
        self.closed = False
        self.waiting = asyncio.Event()
        self.close_calls = 0
        self.running_work_cancelled = False

    def __aiter__(self) -> _ControlledEvents:
        return self

    async def __anext__(self) -> dict[str, Any] | object:
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


class _ControlledRunnable:
    def __init__(self) -> None:
        self.source: _ControlledEvents | None = None

    def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        del input, kwargs
        assert self.source is not None
        return self.source  # type: ignore[return-value]


class _ControlledGraph(_MockCompiledGraph):
    def __init__(self) -> None:
        super().__init__(state=_MockState(values={"messages": []}))
        self.source: _ControlledEvents | None = None

    def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        del input, kwargs
        assert self.source is not None
        return self.source  # type: ignore[return-value]


def _event(
    event_type: str,
    *,
    name: str,
    run_id: str,
    data: dict[str, Any] | None = None,
    parent_ids: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event": event_type,
        "name": name,
        "run_id": run_id,
        "parent_ids": parent_ids or [],
        "data": data or {},
        "metadata": metadata or {},
    }


def _model_stream(text: str) -> dict[str, Any]:
    return _event(
        "on_chat_model_stream",
        name="ChatModel",
        run_id="model-1",
        data={"chunk": _MockAIMessageChunk(content=text)},
    )


def _tool_start() -> dict[str, Any]:
    return _event(
        "on_tool_start",
        name="lookup",
        run_id="tool-1",
        data={"input": {"id": "1"}},
    )


def _tool_end() -> dict[str, Any]:
    return _event(
        "on_tool_end",
        name="lookup",
        run_id="tool-1",
        data={"output": "ok"},
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


def _role(item: Any) -> str:
    raw = item.get("role") if isinstance(item, dict) else getattr(item, "type", "")
    return {"human": "user", "ai": "assistant"}.get(str(raw), str(raw))


def _text(item: Any) -> str:
    raw = item.get("content", "") if isinstance(item, dict) else getattr(item, "content", "")
    return raw if isinstance(raw, str) else str(raw)


class _LangChainFamilyLifecycleDriver:
    """Run one normalized harness against either event-stream bridge."""

    def __init__(self, family: _Family) -> None:
        self.family = family
        self.runnable = _ControlledRunnable()
        self.graph = _ControlledGraph()
        self.bridge: LangChainBridge | LangGraphBridge
        if family == "langchain":
            self.bridge = LangChainBridge(self.runnable)
        else:
            self.bridge = LangGraphBridge(self.graph)
        self.source: _ControlledEvents | None = None

    def _configure(self, items: list[object]) -> _ControlledEvents:
        source = _ControlledEvents(items)
        self.source = source
        if self.family == "langchain":
            self.runnable.source = source
        else:
            self.graph.source = source
        return source

    async def observe_unknown_event_tolerance(self, *, valid_text: str) -> UnknownEventObservation:
        self._configure(
            [
                object(),
                _event("on_future_framework_event", name="future", run_id="future-1"),
                _model_stream(valid_text),
            ]
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
                _model_stream(delivered_text),
                _tool_start(),
                gate,
                _tool_end(),
                _model_stream("must not be delivered"),
            ]
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
        committed_assistant_text = self._last_assistant_text()
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
            [_model_stream("partial"), gate, _model_stream("must not be delivered")]
        )
        stream = self.bridge.invoke(AgentTurnInput.from_text("close"), RecordingAgentRecorder())
        await stream.__anext__()
        await stream.aclose()
        return StreamCloseObservation(
            inner_stream_close_calls=source.close_calls,
            running_work_cancelled=source.running_work_cancelled,
        )

    async def observe_recorder_transient_cleanup(self) -> RecorderCleanupObservation:
        gate = asyncio.Event()
        events: list[object] = []
        if self.family == "langgraph":
            events.append(
                _event(
                    "on_chain_start",
                    name="agent_node",
                    run_id="node-1",
                    metadata={
                        "langgraph_node": "agent_node",
                        "langgraph_step": 1,
                        "langgraph_checkpoint_ns": "",
                    },
                )
            )
        events.extend(
            [
                _event(
                    "on_chat_model_start",
                    name="ChatModel",
                    run_id="model-1",
                    parent_ids=["node-1"] if self.family == "langgraph" else [],
                ),
                _tool_start(),
                gate,
                _tool_end(),
            ]
        )
        source = self._configure(events)
        recorder = RecordingAgentRecorder()
        turn_input = AgentTurnInput.from_text(
            "cleanup",
            context=[{"role": "system", "content": "ephemeral turn context"}],
        )
        stream = self.bridge.invoke(turn_input, recorder)
        await stream.__anext__()
        if self.family == "langgraph":
            assert isinstance(self.bridge, LangGraphBridge)
            assert self.bridge._transient_context_ids
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
        self._seed_prior_turn(prior_user_text, prior_assistant_text)
        before = self._project_prior_history()
        gate = asyncio.Event()
        source = self._configure([gate, _model_stream("must not be delivered")])
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

    def _seed_prior_turn(self, user_text: str, assistant_text: str) -> None:
        if self.family == "langchain":
            assert isinstance(self.bridge, LangChainBridge)
            self.bridge._message_history = [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            ]
            return
        self.graph._state.values["messages"] = [
            _MockMessage("user", user_text, message_id="prior-user"),
            _MockMessage("assistant", assistant_text, message_id="prior-assistant"),
        ]

    def _history(self) -> tuple[NormalizedHistoryEntry, ...]:
        if self.family == "langchain":
            assert isinstance(self.bridge, LangChainBridge)
            messages = self.bridge._message_history
        else:
            messages = self.graph._state.values.get("messages", [])
        return tuple(
            NormalizedHistoryEntry(role=_role(item), text=_text(item)) for item in messages
        )

    def _project_prior_history(self) -> tuple[NormalizedHistoryEntry, ...]:
        return self._history()[:2]

    def _last_assistant_text(self) -> str:
        return next(entry.text for entry in reversed(self._history()) if entry.role == "assistant")

    def _transient_items(self) -> int:
        active_source = int(bool(self.source and self.source.started and not self.source.closed))
        if self.family == "langchain":
            return active_source
        assert isinstance(self.bridge, LangGraphBridge)
        return (
            active_source
            + len(self.bridge._pending_state_mutations)
            + len(self.bridge._transient_context_ids)
        )

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
        if self.family == "langgraph":
            # The production bridge rotates to a fresh checkpoint thread.
            # This one-state test double models that partition explicitly.
            self.graph._state.values["messages"] = []
        self.source = None
        self.runnable.source = None
        self.graph.source = None


def _langchain_driver() -> _LangChainFamilyLifecycleDriver:
    return _LangChainFamilyLifecycleDriver("langchain")


def _langgraph_driver() -> _LangChainFamilyLifecycleDriver:
    return _LangChainFamilyLifecycleDriver("langgraph")


class TestLangChainBridgeLifecycleScenarios(BridgeLifecycleScenarioSuite):
    driver_factory = _langchain_driver


class TestLangGraphBridgeLifecycleScenarios(BridgeLifecycleScenarioSuite):
    driver_factory = _langgraph_driver


def test_langchain_family_suite_scenarios_match_execution_matrix() -> None:
    matrix_path = Path(__file__).with_name("bridge-lifecycle-matrix.json")
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    execution = matrix["driver_execution"]["bridges"]

    assert execution["langchain"]["status"] == "wired"
    assert set(execution["langchain"]["scenarios"]) == (
        TestLangChainBridgeLifecycleScenarios.applicable_scenarios
    )
    assert execution["langgraph"]["status"] == "wired"
    assert set(execution["langgraph"]["scenarios"]) == (
        TestLangGraphBridgeLifecycleScenarios.applicable_scenarios
    )
