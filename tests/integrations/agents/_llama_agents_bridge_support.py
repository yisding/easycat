"""Shared LlamaAgents bridge test doubles and helpers."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import sys
import types
from collections.abc import AsyncIterator
from typing import Any

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import (
    AgentTurnInput,
    BridgeInputError,
    CancellationMode,
    RecorderContext,
)
from easycat.integrations.agents.llama_agents import LlamaAgentsBridge
from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.artifacts import InMemoryArtifactStore

__all__ = [
    "AgentTurnInput",
    "Any",
    "AsyncIterator",
    "BridgeInputError",
    "CancelToken",
    "CancellationMode",
    "InMemoryArtifactStore",
    "InMemoryRingBuffer",
    "JournalAgentRecorder",
    "LlamaAgentsBridge",
    "RecorderContext",
    "_AdvanceThenBlockStream",
    "_BlockingHandler",
    "_BlockingRemoteStream",
    "_BlockingWorkflow",
    "_CancelTrackingHitlHandler",
    "_CancelTrackingHitlWorkflow",
    "_FakeContext",
    "_FakeHandler",
    "_FakeWorkflowBase",
    "_HandlerData",
    "_HitlHandler",
    "_HitlWorkflow",
    "_HumanResponseEvent",
    "_InputRequiredEvent",
    "_LocalWorkflow",
    "_RawStream",
    "_RemoteClient",
    "_RemoteEnvelope",
    "_RemoteHitlClient",
    "_RemoteStream",
    "_SecretContext",
    "_StartEvent",
    "_StopEvent",
    "_TextEvent",
    "_TrackingSource",
    "_recorder",
    "asyncio",
    "contextlib",
    "gc",
    "pytest",
    "sys",
    "types",
]


def _recorder(journal=None):
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


class _FakeWorkflowBase:
    pass


class _StartEvent:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        for key, value in kwargs.items():
            setattr(self, key, value)

    def model_dump(self) -> dict[str, Any]:
        return dict(self.kwargs)


class _StopEvent:
    def __init__(self, result: Any = None) -> None:
        self.result = result


class _InputRequiredEvent:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix


class _HumanResponseEvent:
    def __init__(self, response: str) -> None:
        self.response = response


class _TextEvent:
    def __init__(self, delta: str) -> None:
        self.delta = delta


class _FakeContext:
    def __init__(self, label: str) -> None:
        self.label = label

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label}


class _SecretContext:
    """A workflow Context whose to_dict() carries credentials under
    user-chosen keys, both top-level and nested."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": {
                "username": "ada-lovelace",
                "api_key": "sk-super-secret-value",
                "nested": {
                    "auth_token": "bearer-leak-me",
                    "keep": "non-secret-ok",
                },
            }
        }


class _FakeHandler:
    def __init__(self, events: list[Any], result: Any, ctx: Any = None) -> None:
        self._events = events
        self._result = result
        self.ctx = ctx or _FakeContext("ctx")
        self.run_id = "run-1"
        self.cancelled = False

    def __await__(self):
        async def _result() -> Any:
            return self._result

        return _result().__await__()

    async def stream_events(self) -> AsyncIterator[Any]:
        for event in self._events:
            yield event

    async def cancel_run(self) -> None:
        self.cancelled = True

    def is_done(self) -> bool:
        return self.cancelled


class _LocalWorkflow(_FakeWorkflowBase):
    def __init__(self, *, events: list[Any] | None = None, result: Any = "Hello") -> None:
        self.events = events or []
        self.result = result
        self.calls: list[dict[str, Any]] = []
        self.last_handler: _FakeHandler | None = None
        self.interruption: tuple[str, CancellationMode] | None = None

    def run(self, **kwargs: Any) -> _FakeHandler:
        self.calls.append(kwargs)
        handler = _FakeHandler(
            self.events,
            self.result,
            ctx=_FakeContext(f"ctx-{len(self.calls)}"),
        )
        self.last_handler = handler
        return handler

    def apply_interruption(self, delivered_text: str, mode: CancellationMode) -> None:
        self.interruption = (delivered_text, mode)


class _BlockingHandler:
    """Streams one event, then blocks forever on the next item.

    Mirrors a local workflow stuck on a long step while
    ``stream_events()`` is idle waiting for the next event.
    """

    def __init__(self) -> None:
        self.ctx = _FakeContext("ctx")
        self.run_id = "block-1"
        self.cancelled = False
        self.streaming_blocked = asyncio.Event()
        self._never = asyncio.Event()

    def __await__(self):
        async def _result() -> Any:
            await self._never.wait()
            return "blocked-result"

        return _result().__await__()

    async def stream_events(self) -> AsyncIterator[Any]:
        yield _TextEvent("partial ")
        self.streaming_blocked.set()
        await self._never.wait()
        yield _StopEvent("done")

    async def cancel_run(self) -> None:
        self.cancelled = True
        self._never.set()

    def is_done(self) -> bool:
        return self.cancelled


class _BlockingWorkflow(_FakeWorkflowBase):
    def __init__(self) -> None:
        self.handler = _BlockingHandler()

    def run(self, **kwargs: Any) -> _BlockingHandler:
        return self.handler


class _HitlHandler:
    def __init__(self) -> None:
        self.ctx = self
        self.run_id = "hitl-1"
        self.sent_events: list[Any] = []
        self.stream_calls = 0
        self._response_ready = asyncio.Event()

    def __await__(self):
        async def _result() -> Any:
            return "Thanks Ada"

        return _result().__await__()

    async def stream_events(self, expose_internal: bool = False) -> AsyncIterator[Any]:
        # A real WorkflowHandler exposes a single live event stream: it
        # yields the prompt, stays suspended until ctx.send_event() delivers
        # the human response, then yields the post-response events. It does
        # NOT restart from the prompt on a second stream_events() call, so
        # the bridge must resume this same cursor rather than re-stream.
        self.stream_calls += 1
        yield _InputRequiredEvent(prefix="What is your name?")
        await self._response_ready.wait()
        yield _TextEvent("Thanks ")
        yield _TextEvent(self.sent_events[-1].response)
        yield _StopEvent("done")

    def send_event(self, event: Any, step: str | None = None) -> None:
        self.sent_events.append(event)
        self._response_ready.set()


class _HitlWorkflow(_FakeWorkflowBase):
    def __init__(self) -> None:
        self.handler = _HitlHandler()

    def run(self, **kwargs: Any) -> _HitlHandler:
        return self.handler


class _CancelTrackingHitlHandler(_HitlHandler):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled = False

    async def cancel_run(self) -> None:
        self.cancelled = True


class _CancelTrackingHitlWorkflow(_FakeWorkflowBase):
    def __init__(self) -> None:
        self.handler = _CancelTrackingHitlHandler()

    def run(self, **kwargs: Any) -> _CancelTrackingHitlHandler:
        return self.handler


class _RemoteEnvelope:
    def __init__(self, event: Any) -> None:
        self._event = event
        self.type = type(event).__name__
        self.value = getattr(event, "__dict__", {})

    def load_event(self) -> Any:
        return self._event


class _HandlerData:
    def __init__(
        self,
        handler_id: str,
        result: Any = None,
        context: Any = None,
        status: Any = None,
        error: Any = None,
    ) -> None:
        self.handler_id = handler_id
        self.result = result
        self.context = context
        self.status = status
        self.error = error


class _RemoteStream:
    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self.closed = False
        self.last_sequence = -1

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Any]:
        for sequence, event in enumerate(self._events):
            self.last_sequence = sequence
            yield _RemoteEnvelope(event)

    async def aclose(self) -> None:
        self.closed = True


class _RawStream:
    """Yields pre-built envelopes verbatim (no _RemoteEnvelope wrapping)."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self.closed = False
        self.last_sequence = -1

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Any]:
        for sequence, item in enumerate(self._items):
            self.last_sequence = sequence
            yield item

    async def aclose(self) -> None:
        self.closed = True


class _BlockingRemoteStream:
    """Yields one envelope, then blocks forever on the next item."""

    def __init__(self) -> None:
        self.closed = False
        self.last_sequence = -1
        self.streaming_blocked = asyncio.Event()
        self._never = asyncio.Event()

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Any]:
        self.last_sequence = 0
        yield _RemoteEnvelope(_TextEvent("remote partial"))
        self.streaming_blocked.set()
        await self._never.wait()
        yield _RemoteEnvelope(_StopEvent("done"))

    async def aclose(self) -> None:
        self.closed = True
        self._never.set()


class _AdvanceThenBlockStream:
    """Delivers event 0, then advances ``last_sequence`` to 1 and blocks
    *before* yielding event 1.

    A barge-in during the block drops the undelivered event 1 even though
    ``last_sequence`` has already moved past it -- mirroring the
    ``_aiter_with_cancellation`` race where the loop body never runs for an
    event whose sequence the stream already counted.
    """

    def __init__(self) -> None:
        self.closed = False
        self.last_sequence = -1
        self.first_delivered = asyncio.Event()
        self._never = asyncio.Event()

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Any]:
        self.last_sequence = 0
        yield _RemoteEnvelope(_TextEvent("first delta"))
        self.first_delivered.set()
        self.last_sequence = 1
        await self._never.wait()
        yield _RemoteEnvelope(_TextEvent("interrupted second delta"))

    async def aclose(self) -> None:
        self.closed = True
        self._never.set()


class _RemoteClient:
    def __init__(self) -> None:
        self.run_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []
        self.sent_events: list[tuple[str, Any]] = []
        self.cancelled: list[str] = []

    async def run_workflow_nowait(self, workflow_name: str, **kwargs: Any) -> _HandlerData:
        self.run_calls.append({"workflow_name": workflow_name, **kwargs})
        return _HandlerData("h1")

    def get_workflow_events(self, handler_id: str, **kwargs: Any) -> _RemoteStream:
        self.stream_calls.append({"handler_id": handler_id, **kwargs})
        return _RemoteStream([_TextEvent("remote "), _TextEvent("text"), _StopEvent("done")])

    async def get_handler(self, handler_id: str) -> _HandlerData:
        return _HandlerData(handler_id, result="remote text", context={"saved": True})

    async def cancel_handler(self, handler_id: str) -> None:
        self.cancelled.append(handler_id)

    async def send_event(self, handler_id: str, event: Any, step: str | None = None) -> None:
        self.sent_events.append((handler_id, event))


class _RemoteHitlClient(_RemoteClient):
    def get_workflow_events(self, handler_id: str, **kwargs: Any) -> _RemoteStream:
        self.stream_calls.append({"handler_id": handler_id, **kwargs})
        if len(self.stream_calls) == 1:
            return _RemoteStream([_InputRequiredEvent(prefix="Remote prompt")])
        return _RemoteStream([_TextEvent("Remote "), _TextEvent("done"), _StopEvent("done")])

    async def get_handler(self, handler_id: str) -> _HandlerData:
        return _HandlerData(handler_id, result="Remote done", context={"saved": True})


class _TrackingSource:
    """Async iterator whose generator runs a ``finally`` only when closed.

    Mirrors a local workflow ``stream_events()`` generator: it yields one
    item, then blocks idle waiting for the next step (so it is never
    naturally drained). ``closed`` flips True only if the generator's
    cleanup actually runs -- i.e. only if the wrapper closed the iterator.
    """

    def __init__(self) -> None:
        self.closed = False
        self.exhausted = False
        self.read_task_names: list[str] = []
        self._never = asyncio.Event()

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Any]:
        try:
            task = asyncio.current_task()
            assert task is not None
            self.read_task_names.append(task.get_name())
            yield "first"
            task = asyncio.current_task()
            assert task is not None
            self.read_task_names.append(task.get_name())
            await self._never.wait()
            yield "second"
            self.exhausted = True
        finally:
            self.closed = True
