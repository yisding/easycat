"""Shared LangGraph bridge test doubles and helpers."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import AsyncIterator
from typing import Any

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents._agent_runner import AgentRunner, AgentRunnerConfig
from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import (
    AgentTurnInput,
    BridgeInputError,
    CancellationMode,
    CommitRule,
    RecorderContext,
    UnitKind,
)
from easycat.integrations.agents.langgraph import LangGraphBridge
from easycat.runtime import InMemoryRingBuffer
from easycat.timeouts import AgentTimeoutError

__all__ = [
    "asyncio",
    "functools",
    "AsyncIterator",
    "Any",
    "pytest",
    "CancelToken",
    "AgentRunner",
    "AgentRunnerConfig",
    "JournalAgentRecorder",
    "AgentTurnInput",
    "BridgeInputError",
    "CancellationMode",
    "CommitRule",
    "RecorderContext",
    "UnitKind",
    "LangGraphBridge",
    "InMemoryRingBuffer",
    "AgentTimeoutError",
    "_MockAIMessageChunk",
    "_MockMessage",
    "_MockState",
    "_MockCheckpointer",
    "_MockCompiledGraph",
    "_node_start",
    "_node_end",
    "_model_stream",
    "_recorder",
    "_id_of",
    "_content",
    "LastValue",
    "_fake_add_messages",
    "_ReducerChannel",
    "_GenericReducerChannel",
    "_CancelAfter",
    "_BoundConfigGraph",
    "_ConfigRecordingGraph",
    "_FormattedAddMessagesChannel",
]


class _MockAIMessageChunk:
    def __init__(self, content: str = "") -> None:
        self.content = content
        self.tool_call_chunks: list[Any] = []
        self.id = "c"
        self.type = "ai"


class _MockMessage:
    def __init__(self, role: str, content: str, message_id: str | None = None) -> None:
        self.type = {"assistant": "ai", "user": "human", "system": "system"}.get(role, role)
        self.content = content
        self.id = message_id


class _MockState:
    def __init__(
        self,
        values: dict[str, Any] | None = None,
        checkpoint_id: str = "cp-1",
        thread_id: str = "t-1",
    ) -> None:
        self.values = values or {}
        self.config = {"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}}
        self.metadata = {"step": 1}
        self.next: tuple[str, ...] = ()
        self.tasks: tuple[Any, ...] = ()
        self.interrupts: tuple[Any, ...] = ()


class _MockCheckpointer:
    """Marker — LangGraphBridge only probes ``graph.checkpointer``."""


class _MockCompiledGraph:
    """Duck-types ``langgraph.graph.state.CompiledStateGraph``.

    Emits scripted ``astream_events(version="v2")`` dicts.  Tests build
    the script directly; helpers below make the common shapes easy.
    """

    def __init__(
        self,
        scripted: list[dict[str, Any]] | None = None,
        *,
        state: _MockState | None = None,
        state_history: list[_MockState] | None = None,
    ) -> None:
        self._scripted = scripted or []
        self.checkpointer = _MockCheckpointer()
        self._state = state or _MockState(values={"messages": []})
        # ``get_state_history`` payload, newest→oldest (as real
        # LangGraph yields).  Mutable so multi-turn tests can grow it
        # between invocations.  Defaults to just the final state.
        self.state_history = state_history
        self.update_state_calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def astream_events(
        self,
        input: Any,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        async def _gen() -> AsyncIterator[dict[str, Any]]:
            for event in self._scripted:
                yield event

        return _gen()

    def get_state(self, config: dict[str, Any]) -> _MockState:
        return self._state

    def get_state_history(self, config: dict[str, Any]) -> Any:
        history = self.state_history if self.state_history is not None else [self._state]
        return iter(list(history))

    def update_state(self, config: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        self.update_state_calls.append((config, values))
        # Simulate ``add_messages``: dedupe by id when present.
        key = "messages"
        existing = list(self._state.values.get(key, []))
        for new_msg in values.get(key, []):
            new_id = getattr(new_msg, "id", None) or (
                new_msg.get("id") if isinstance(new_msg, dict) else None
            )
            if new_id:
                replaced = False
                for i, old in enumerate(existing):
                    old_id = getattr(old, "id", None) or (
                        old.get("id") if isinstance(old, dict) else None
                    )
                    if old_id == new_id:
                        existing[i] = new_msg
                        replaced = True
                        break
                if replaced:
                    continue
            existing.append(new_msg)
        self._state.values[key] = existing
        return {"configurable": {"thread_id": "t-1", "checkpoint_id": "cp-2"}}


def _node_start(
    node: str, run_id: str, checkpoint_id: str = "cp-1", step: int = 1
) -> dict[str, Any]:
    return {
        "event": "on_chain_start",
        "name": node,
        "run_id": run_id,
        "parent_ids": [],
        "data": {},
        "metadata": {
            "langgraph_node": node,
            "langgraph_step": step,
            "langgraph_checkpoint_ns": "",
            "checkpoint_id": checkpoint_id,
            "thread_id": "t-1",
        },
    }


def _node_end(node: str, run_id: str, checkpoint_id: str = "cp-1") -> dict[str, Any]:
    return {
        "event": "on_chain_end",
        "name": node,
        "run_id": run_id,
        "parent_ids": [],
        "data": {},
        "metadata": {
            "langgraph_node": node,
            "langgraph_checkpoint_ns": "",
            "checkpoint_id": checkpoint_id,
        },
    }


def _model_stream(
    text: str,
    *,
    run_id: str = "m",
    parent: str = "",
    node: str | None = None,
    checkpoint_id: str = "cp-1",
) -> dict[str, Any]:
    meta: dict[str, Any] = {"checkpoint_id": checkpoint_id}
    if node is not None:
        meta["langgraph_node"] = node
    return {
        "event": "on_chat_model_stream",
        "name": "ChatOpenAI",
        "run_id": run_id,
        "parent_ids": [parent] if parent else [],
        "data": {"chunk": _MockAIMessageChunk(content=text)},
        "metadata": meta,
    }


def _recorder(journal: InMemoryRingBuffer | None = None) -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


def _id_of(m: Any) -> Any:
    return getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)


def _content(m: Any) -> Any:
    return m.get("content") if isinstance(m, dict) else getattr(m, "content", None)


class LastValue:
    """Duck-types LangGraph's plain (no-reducer) ``LastValue`` channel.

    Named exactly ``LastValue`` because the bridge positively
    identifies a no-reducer channel by ``type(channel).__name__``.
    """


def _fake_add_messages(*args: Any, **kwargs: Any) -> Any:
    return args, kwargs


class _ReducerChannel:
    """Duck-types an ``Annotated[list, add_messages]`` reducer channel.

    Its ``.operator`` has the same name shape as LangGraph's
    ``add_messages`` reducer so these duck-typed tests do not need the
    real optional package installed."""

    def __init__(self) -> None:
        self.operator = _fake_add_messages


class _GenericReducerChannel:
    """Duck-types ``Annotated[list, operator.add]`` (a non-``add_messages``
    accumulator): it only ever *appends*, so a ``RemoveMessage`` marker
    or id-keyed re-send would be appended as a fresh tail rather than
    merged — the bridge must treat it like a no-reducer channel."""

    def __init__(self) -> None:
        import operator

        self.operator = operator.add


class _CancelAfter:
    """Cancel-token double whose ``is_cancelled`` returns ``False`` for
    the first ``n`` checks, then ``True`` — deterministically simulating
    a barge-in tripped *after* some text has already streamed (the real
    ``CancelToken`` would set its event asynchronously)."""

    def __init__(self, n: int) -> None:
        self._remaining = n

    @property
    def is_cancelled(self) -> bool:
        if self._remaining > 0:
            self._remaining -= 1
            return False
        return True


class _BoundConfigGraph(_MockCompiledGraph):
    """Duck-types a graph carrying a config bound via
    ``graph.with_config(configurable={"thread_id": ...})`` — LangGraph
    stores the merged config on ``graph.config``."""

    def __init__(self, thread_id: str, **kwargs: Any) -> None:
        super().__init__([], **kwargs)
        self.config = {"configurable": {"thread_id": thread_id}}


class _ConfigRecordingGraph(_MockCompiledGraph):
    """Duck-types a graph bound via ``graph.with_config(configurable=
    {"thread_id": ..., "checkpoint_id": ...})`` — LangGraph's resume /
    time-travel config.  Records the ``checkpoint_id`` seen on every
    ``get_state`` and ``astream_events`` call."""

    def __init__(self, thread_id: str, checkpoint_id: str, **kwargs: Any) -> None:
        super().__init__([], **kwargs)
        self.config = {"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}}
        self.get_state_cps: list[Any] = []
        self.astream_cps: list[Any] = []

    @staticmethod
    def _cp(config: dict[str, Any]) -> Any:
        return (config.get("configurable") or {}).get("checkpoint_id")

    def get_state(self, config: dict[str, Any]) -> _MockState:
        self.get_state_cps.append(self._cp(config))
        return super().get_state(config)

    def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        self.astream_cps.append(self._cp(kwargs.get("config") or {}))
        return super().astream_events(input, **kwargs)


class _FormattedAddMessagesChannel:
    """Duck-types ``Annotated[list, add_messages(format="langchain-openai")]``
    — LangGraph stores the reducer as ``functools.partial(add_messages,
    ...)``, which is still genuine ``add_messages`` merge semantics."""

    def __init__(self) -> None:
        self.operator = functools.partial(_fake_add_messages, format="langchain-openai")
