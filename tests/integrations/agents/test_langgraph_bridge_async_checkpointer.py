"""Async-first LangGraph checkpointer access and degradation visibility."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from ._langgraph_bridge_support import (
    AgentTurnInput,
    BridgeInputError,
    CancellationMode,
    InMemoryRingBuffer,
    LangGraphBridge,
    _MockCompiledGraph,
    _MockMessage,
    _MockState,
    _model_stream,
    _recorder,
)


class _AsyncOnlyGraph(_MockCompiledGraph):
    def __init__(self) -> None:
        state = _MockState(
            values={
                "messages": [
                    _MockMessage("user", "question"),
                    _MockMessage("assistant", "partial answer", message_id="answer"),
                ]
            },
            checkpoint_id="cp-1",
        )
        super().__init__([_model_stream("partial answer")], state=state)
        self.async_get_calls = 0
        self.async_update_calls: list[dict[str, Any]] = []
        self.async_update_configs: list[dict[str, Any]] = []
        self.async_history_calls = 0
        self.sync_calls: list[str] = []

    def get_state(self, config: dict[str, Any]) -> _MockState:
        self.sync_calls.append("get_state")
        raise AssertionError("sync get_state must not be called")

    def update_state(self, config: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        self.sync_calls.append("update_state")
        raise AssertionError("sync update_state must not be called")

    def get_state_history(self, config: dict[str, Any]) -> Any:
        self.sync_calls.append("get_state_history")
        raise AssertionError("sync get_state_history must not be called")

    async def aget_state(self, config: dict[str, Any]) -> _MockState:
        self.async_get_calls += 1
        return self._state

    async def aupdate_state(
        self, config: dict[str, Any], values: dict[str, Any]
    ) -> dict[str, Any]:
        self.async_update_configs.append(config)
        self.async_update_calls.append(values)
        for new_msg in values.get("messages", []):
            new_id = getattr(new_msg, "id", None)
            messages = self._state.values["messages"]
            for index, old_msg in enumerate(messages):
                if new_id and getattr(old_msg, "id", None) == new_id:
                    messages[index] = new_msg
                    break
            else:
                messages.append(new_msg)
        return {"configurable": {"thread_id": "t-1", "checkpoint_id": "cp-2"}}

    async def aget_state_history(self, config: dict[str, Any]):
        self.async_history_calls += 1
        yield self._state


@pytest.mark.asyncio
async def test_async_only_graph_uses_async_state_apis_and_flushes_queued_rewrite() -> None:
    graph = _AsyncOnlyGraph()
    bridge = LangGraphBridge(graph)

    async for _ in bridge.invoke(AgentTurnInput.from_text("one"), _recorder()):
        pass

    assert graph.async_get_calls
    assert graph.async_history_calls
    assert graph.sync_calls == []

    bridge.apply_interruption("partial", CancellationMode.IMMEDIATE_STOP)
    bridge.append_interruption_note("[user interrupted]")
    assert graph.async_update_calls == []

    async for _ in bridge.invoke(AgentTurnInput.from_text("two"), _recorder()):
        pass

    assert len(graph.async_update_calls) == 2
    assert graph._state.values["messages"][-2].content == "partial..."
    note = graph._state.values["messages"][-1]
    note_content = note.get("content") if isinstance(note, dict) else note.content
    assert note_content == "[user interrupted]"
    assert graph.sync_calls == []


@pytest.mark.asyncio
async def test_aclose_flushes_final_rewrite_and_interruption_note() -> None:
    graph = _AsyncOnlyGraph()
    bridge = LangGraphBridge(graph)
    async for _ in bridge.invoke(AgentTurnInput.from_text("one"), _recorder()):
        pass

    bridge.apply_interruption("partial", CancellationMode.IMMEDIATE_STOP)
    bridge.append_interruption_note("[user interrupted]")

    await bridge.aclose()

    assert len(graph.async_update_calls) == 2
    assert graph._state.values["messages"][-2].content == "partial..."
    note = graph._state.values["messages"][-1]
    note_content = note.get("content") if isinstance(note, dict) else note.content
    assert note_content == "[user interrupted]"
    assert bridge._pending_state_mutations == []


@pytest.mark.asyncio
async def test_reset_preserves_pending_write_for_original_thread() -> None:
    graph = _AsyncOnlyGraph()
    bridge = LangGraphBridge(graph)
    async for _ in bridge.invoke(AgentTurnInput.from_text("one"), _recorder()):
        pass
    original_thread_id = bridge._thread_id

    bridge.replace_last_assistant_text("normalized")
    bridge.reset()
    assert bridge._thread_id != original_thread_id

    await bridge.aclose()

    flushed_config = graph.async_update_configs[-1]["configurable"]
    assert flushed_config["thread_id"] == original_thread_id
    assert graph._state.values["messages"][-1].content == "normalized"
    assert bridge._last_checkpoint_id is None


class _ThreadTrackingGraph(_MockCompiledGraph):
    def __init__(self) -> None:
        super().__init__([_model_stream("answer")])
        self.state_threads: list[int] = []
        self.history_threads: list[int] = []

    def get_state(self, config: dict[str, Any]) -> _MockState:
        self.state_threads.append(threading.get_ident())
        return super().get_state(config)

    def get_state_history(self, config: dict[str, Any]) -> Any:
        self.history_threads.append(threading.get_ident())
        return super().get_state_history(config)


@pytest.mark.asyncio
async def test_sync_state_apis_run_outside_the_event_loop_thread() -> None:
    event_loop_thread = threading.get_ident()
    graph = _ThreadTrackingGraph()

    async for _ in LangGraphBridge(graph).invoke(
        AgentTurnInput.from_text("question"), _recorder()
    ):
        pass

    assert graph.state_threads
    assert graph.history_threads
    assert all(thread_id != event_loop_thread for thread_id in graph.state_threads)
    assert all(thread_id != event_loop_thread for thread_id in graph.history_threads)


class _BrokenAsyncGraph(_AsyncOnlyGraph):
    async def aget_state(self, config: dict[str, Any]) -> _MockState:
        raise RuntimeError("async checkpointer unavailable")


@pytest.mark.asyncio
async def test_async_state_failure_is_recorded_in_the_framework_journal() -> None:
    journal = InMemoryRingBuffer(capacity=100)
    bridge = LangGraphBridge(_BrokenAsyncGraph())

    async for _ in bridge.invoke(AgentTurnInput.from_text("question"), _recorder(journal)):
        pass

    framework_errors = [record for record in journal.read() if record.name == "framework_error"]
    assert framework_errors
    assert framework_errors[-1].error is not None
    assert framework_errors[-1].error.message == "async checkpointer unavailable"


@pytest.mark.asyncio
async def test_real_compiled_graph_runs_through_async_state_surface() -> None:
    pytest.importorskip("langgraph")
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, MessagesState, StateGraph

    async def answer(state: MessagesState) -> dict[str, list[AIMessage]]:
        return {"messages": [AIMessage(content="real graph answer")]}

    builder = StateGraph(MessagesState)
    builder.add_node("answer", answer)
    builder.add_edge(START, "answer")
    builder.add_edge("answer", END)
    bridge = LangGraphBridge(builder.compile(checkpointer=InMemorySaver()))

    events = [
        event async for event in bridge.invoke(AgentTurnInput.from_text("question"), _recorder())
    ]

    assert bridge._async_state_surface
    assert events[-1].kind == "done"
    assert events[-1].text == "real graph answer"


def test_real_checkpointer_with_no_state_api_is_rejected() -> None:
    pytest.importorskip("langgraph")
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph import END, START, StateGraph

    class EmptySaver(BaseCheckpointSaver):
        pass

    builder = StateGraph(dict)
    builder.add_node("answer", lambda state: state)
    builder.add_edge(START, "answer")
    builder.add_edge("answer", END)
    graph = builder.compile(checkpointer=EmptySaver())

    with pytest.raises(BridgeInputError, match="neither get_tuple\\(\\) nor aget_tuple"):
        LangGraphBridge(graph)
