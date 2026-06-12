"""Shared LangChain bridge test doubles and helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents._agent_runner import AgentRunner, AgentRunnerConfig
from easycat.integrations.agents._langchain_events import translate_stream_event
from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import (
    NULL_RECORDER,
    AgentTurnInput,
    BridgeInputError,
    CancellationMode,
    CommitRule,
    RecorderContext,
    UnitKind,
)
from easycat.integrations.agents.langchain import LangChainBridge
from easycat.runtime import InMemoryRingBuffer
from easycat.timeouts import AgentTimeoutError

__all__ = [
    "asyncio",
    "AsyncIterator",
    "SimpleNamespace",
    "Any",
    "pytest",
    "CancelToken",
    "AgentRunner",
    "AgentRunnerConfig",
    "translate_stream_event",
    "JournalAgentRecorder",
    "NULL_RECORDER",
    "AgentTurnInput",
    "BridgeInputError",
    "CancellationMode",
    "CommitRule",
    "RecorderContext",
    "UnitKind",
    "LangChainBridge",
    "InMemoryRingBuffer",
    "AgentTimeoutError",
    "_MockAIMessageChunk",
    "_MockRunnable",
    "_recorder",
    "_content_of_history_item",
    "_role_of_msg",
    "_InMemoryStore",
    "_FakeHistoryRunnable",
    "_CopyReturningStore",
    "_CopyStoreHistoryRunnable",
    "_CustomKeyHistoryRunnable",
    "_SingleCustomKeyHistoryRunnable",
]


class _MockAIMessageChunk:
    """Duck-types as ``langchain_core.messages.AIMessageChunk``."""

    def __init__(
        self,
        content: str = "",
        tool_call_chunks: list[dict[str, Any]] | None = None,
    ) -> None:
        self.content = content
        self.tool_call_chunks = tool_call_chunks or []
        self.id = "chunk-id"


class _MockRunnable:
    """Duck-types as ``langchain_core.runnables.Runnable``.

    Only implements the subset ``LangChainBridge`` relies on:
    ``astream_events(input, version=...)``.
    """

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events
        self.invoked_with: Any = None

    async def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        self.invoked_with = (input, kwargs)
        for event in self._events:
            yield event

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any: ...


def _recorder(journal: InMemoryRingBuffer | None = None) -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


def _content_of_history_item(item: Any) -> Any:
    """Tolerate both dict-shaped and typed-message history items."""
    if isinstance(item, dict):
        return item.get("content")
    return getattr(item, "content", None)


def _role_of_msg(item: Any) -> Any:
    """Tolerate dict-shaped and typed-message items (role accessor)."""
    if isinstance(item, dict):
        return item.get("role")
    return getattr(item, "type", None)  # langchain messages expose ``.type``


class _InMemoryStore:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def add_message(self, m: Any) -> None:
        self.messages.append(m)

    def add_messages(self, ms: list[Any]) -> None:
        self.messages.extend(ms)

    def clear(self) -> None:
        self.messages.clear()


class _FakeHistoryRunnable:
    """Duck-types ``RunnableWithMessageHistory``: each turn persists the
    user input + model output into a per-session store and (the real
    wrapper) rebuilds the prompt history from it, *overwriting* the
    bridge's ``history`` key.  So shadow-list-only edits are invisible
    to the next turn unless mirrored into this store."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.history_factory_config: list[Any] = []
        self._stores: dict[str, _InMemoryStore] = {}

    def get_session_history(self, session_id: str) -> _InMemoryStore:
        return self._stores.setdefault(session_id, _InMemoryStore())

    async def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        sid = kwargs["config"]["configurable"]["session_id"]
        store = self.get_session_history(sid)
        store.add_message({"role": "user", "content": "q"})
        yield {
            "event": "on_chat_model_stream",
            "name": "ChatOpenAI",
            "run_id": "m",
            "parent_ids": [],
            "data": {"chunk": _MockAIMessageChunk(content=self._reply)},
        }
        store.add_message({"role": "assistant", "content": self._reply})

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any: ...


class _CopyReturningStore:
    """Duck-types a backend-backed ``BaseChatMessageHistory`` (SQL /
    Redis / file): ``.messages`` rebuilds a *fresh copy* (new list, new
    dict objects) from the backend on every access, so an in-place edit
    of the returned list never reaches the backend.  Only ``clear()`` +
    ``add_messages()`` mutate the backend."""

    def __init__(self) -> None:
        self._backend: list[Any] = []

    @property
    def messages(self) -> list[Any]:
        return [dict(m) if isinstance(m, dict) else m for m in self._backend]

    def add_message(self, m: Any) -> None:
        self._backend.append(dict(m) if isinstance(m, dict) else m)

    def add_messages(self, ms: list[Any]) -> None:
        for m in ms:
            self.add_message(m)

    def clear(self) -> None:
        self._backend.clear()


class _CopyStoreHistoryRunnable:
    """``RunnableWithMessageHistory`` double whose backing store returns
    a fetched copy from ``.messages`` (mirrors real DB-backed stores)."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.history_factory_config: list[Any] = []
        self._stores: dict[str, _CopyReturningStore] = {}

    def get_session_history(self, session_id: str) -> _CopyReturningStore:
        return self._stores.setdefault(session_id, _CopyReturningStore())

    async def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        sid = kwargs["config"]["configurable"]["session_id"]
        store = self.get_session_history(sid)
        store.add_message({"role": "user", "content": "q"})
        yield {
            "event": "on_chat_model_stream",
            "name": "ChatOpenAI",
            "run_id": "m",
            "parent_ids": [],
            "data": {"chunk": _MockAIMessageChunk(content=self._reply)},
        }
        store.add_message({"role": "assistant", "content": self._reply})

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any: ...


class _CustomKeyHistoryRunnable:
    """Duck-types ``RunnableWithMessageHistory`` wrapped with a custom
    ``history_factory_config`` (``user_id`` / ``conversation_id`` instead
    of ``session_id``): ``get_session_history`` is keyword-only and the
    store is keyed by the tuple of those configurable values."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.history_factory_config = [
            SimpleNamespace(id="user_id"),
            SimpleNamespace(id="conversation_id"),
        ]
        self._stores: dict[tuple[str, str], _InMemoryStore] = {}

    def get_session_history(self, *, user_id: str, conversation_id: str) -> _InMemoryStore:
        return self._stores.setdefault((user_id, conversation_id), _InMemoryStore())

    async def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        cfg = kwargs["config"]["configurable"]
        store = self.get_session_history(
            user_id=cfg["user_id"], conversation_id=cfg["conversation_id"]
        )
        store.add_message({"role": "user", "content": "q"})
        yield {
            "event": "on_chat_model_stream",
            "name": "ChatOpenAI",
            "run_id": "m",
            "parent_ids": [],
            "data": {"chunk": _MockAIMessageChunk(content=self._reply)},
        }
        store.add_message({"role": "assistant", "content": self._reply})

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any: ...


class _SingleCustomKeyHistoryRunnable:
    """Duck-types ``RunnableWithMessageHistory`` wrapped with a *single*
    custom ``history_factory_config`` key (``conversation_id``).  Like
    real LangChain, the store is keyed by that key's *value* and the
    factory takes one positional arg — so probing it with the
    synthesized ``session_id`` (the pre-fix behaviour) succeeds without
    a ``TypeError`` and silently resolves a *different* store."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.history_factory_config = [SimpleNamespace(id="conversation_id")]
        self._stores: dict[str, _InMemoryStore] = {}

    def get_session_history(self, conversation_id: str) -> _InMemoryStore:
        return self._stores.setdefault(conversation_id, _InMemoryStore())

    async def astream_events(self, input: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        cfg = kwargs["config"]["configurable"]
        store = self.get_session_history(cfg["conversation_id"])
        store.add_message({"role": "user", "content": "q"})
        yield {
            "event": "on_chat_model_stream",
            "name": "ChatOpenAI",
            "run_id": "m",
            "parent_ids": [],
            "data": {"chunk": _MockAIMessageChunk(content=self._reply)},
        }
        store.add_message({"role": "assistant", "content": self._reply})

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any: ...
