"""Regression coverage for in-flight tool drain across LC event bridges."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents.base import AgentTurnInput, CancellationMode
from easycat.integrations.agents.langchain import LangChainBridge
from easycat.integrations.agents.langgraph import LangGraphBridge
from easycat.testing import RecordingAgentRecorder

from ._langchain_bridge_support import _content_of_history_item, _MockAIMessageChunk
from ._langgraph_bridge_support import (
    _content,
    _MockCompiledGraph,
    _MockMessage,
    _MockState,
)


class _CloseAwareEvents:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = iter(events)
        self.closed = False

    def __aiter__(self) -> _CloseAwareEvents:
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self) -> None:
        self.closed = True


class _ControlledRunnable:
    def __init__(self, events: _CloseAwareEvents) -> None:
        self.events = events

    def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        del input, kwargs
        return self.events


class _ControlledGraph(_MockCompiledGraph):
    def __init__(self, events: _CloseAwareEvents, state: _MockState) -> None:
        super().__init__(state=state)
        self.events = events

    def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        del input, kwargs
        return self.events


def _events(delivered_text: str) -> _CloseAwareEvents:
    return _CloseAwareEvents(
        [
            {
                "event": "on_chat_model_stream",
                "name": "ChatModel",
                "run_id": "model-1",
                "parent_ids": [],
                "data": {"chunk": _MockAIMessageChunk(content=delivered_text)},
                "metadata": {},
            },
            {
                "event": "on_tool_start",
                "name": "lookup",
                "run_id": "tool-1",
                "parent_ids": [],
                "data": {"input": {"id": "1"}},
                "metadata": {},
            },
            {
                "event": "on_tool_end",
                "name": "lookup",
                "run_id": "tool-1",
                "parent_ids": [],
                "data": {"output": "ok"},
                "metadata": {},
            },
            {
                "event": "on_chat_model_stream",
                "name": "ChatModel",
                "run_id": "model-1",
                "parent_ids": [],
                "data": {"chunk": _MockAIMessageChunk(content="must not be delivered")},
                "metadata": {},
            },
        ]
    )


async def _drive_cancel_after_tool_start(
    bridge: LangChainBridge | LangGraphBridge,
) -> tuple[list[str], RecordingAgentRecorder]:
    token = CancelToken()
    recorder = RecordingAgentRecorder()
    kinds: list[str] = []
    async for event in bridge.invoke(
        AgentTurnInput.from_text("current question"),
        recorder,
        cancel_token=token,
    ):
        kinds.append(event.kind)
        if event.kind == "tool_started":
            token.cancel()
    return kinds, recorder


@pytest.mark.asyncio
async def test_langchain_cancel_drains_pending_tool_result_before_done() -> None:
    delivered = "delivered partial"
    events = _events(delivered)
    bridge = LangChainBridge(_ControlledRunnable(events))
    bridge._message_history = [
        {"role": "user", "content": "prior question"},
        {"role": "assistant", "content": "prior answer"},
    ]

    kinds, recorder = await _drive_cancel_after_tool_start(bridge)

    assert kinds == ["text_delta", "tool_started", "tool_result", "done"]
    assert recorder.tool_phases() == ["start", "result"]
    assert events.closed
    assert _content_of_history_item(bridge._message_history[-1]) == delivered
    bridge.apply_interruption(delivered, CancellationMode.IMMEDIATE_STOP)
    assert _content_of_history_item(bridge._message_history[1]) == "prior answer"
    assert _content_of_history_item(bridge._message_history[-1]) == delivered + "..."


@pytest.mark.asyncio
async def test_langgraph_cancel_drains_pending_tool_result_before_done() -> None:
    delivered = "delivered partial"
    events = _events(delivered)
    prior = _MockMessage("assistant", "prior answer", message_id="prior-ai")
    state = _MockState(values={"messages": [_MockMessage("user", "prior question"), prior]})
    graph = _ControlledGraph(events, state)
    bridge = LangGraphBridge(graph)

    kinds, recorder = await _drive_cancel_after_tool_start(bridge)

    assert kinds == ["text_delta", "tool_started", "tool_result", "done"]
    assert recorder.tool_phases() == ["start", "result"]
    assert events.closed
    assert _content(graph._state.values["messages"][-1]) == delivered
    bridge.apply_interruption(delivered, CancellationMode.IMMEDIATE_STOP)
    assert _content(prior) == "prior answer"
    assert _content(graph._state.values["messages"][-1]) == delivered + "..."
