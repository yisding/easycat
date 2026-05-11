"""LlamaAgentsBridge tests using local fakes instead of provider packages."""

from __future__ import annotations

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
from easycat.runtime.journal import InMemoryRingBuffer


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


@pytest.fixture
def fake_workflows_modules(monkeypatch):
    workflows = types.ModuleType("workflows")
    workflows.Workflow = _FakeWorkflowBase
    workflows.StartEvent = _StartEvent
    workflows.StopEvent = _StopEvent
    workflows.InputRequiredEvent = _InputRequiredEvent
    workflows.HumanResponseEvent = _HumanResponseEvent
    events = types.ModuleType("workflows.events")
    events.StartEvent = _StartEvent
    events.StopEvent = _StopEvent
    events.InputRequiredEvent = _InputRequiredEvent
    events.HumanResponseEvent = _HumanResponseEvent
    monkeypatch.setitem(sys.modules, "workflows", workflows)
    monkeypatch.setitem(sys.modules, "workflows.events", events)


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


class _HitlHandler:
    def __init__(self) -> None:
        self.ctx = self
        self.run_id = "hitl-1"
        self.sent_events: list[Any] = []
        self.stream_calls = 0

    def __await__(self):
        async def _result() -> Any:
            return "Thanks Ada"

        return _result().__await__()

    async def stream_events(self, expose_internal: bool = False) -> AsyncIterator[Any]:
        self.stream_calls += 1
        if self.stream_calls == 1:
            yield _InputRequiredEvent(prefix="What is your name?")
            return
        yield _TextEvent("Thanks ")
        yield _TextEvent(self.sent_events[-1].response)
        yield _StopEvent("done")

    def send_event(self, event: Any, step: str | None = None) -> None:
        self.sent_events.append(event)


class _HitlWorkflow(_FakeWorkflowBase):
    def __init__(self) -> None:
        self.handler = _HitlHandler()

    def run(self, **kwargs: Any) -> _HitlHandler:
        return self.handler


class TestLocalLlamaAgentsBridge:
    @pytest.mark.asyncio
    async def test_streams_workflow_events_and_records_cursor(self, fake_workflows_modules):
        workflow = _LocalWorkflow(
            events=[_TextEvent("Hel"), _TextEvent("lo"), _StopEvent("Hello")]
        )
        bridge = LlamaAgentsBridge(workflow=workflow)
        journal = InMemoryRingBuffer(capacity=1000)

        events = []
        async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder(journal)):
            events.append(event)

        assert [event.text for event in events if event.kind == "text_delta"] == ["Hel", "lo"]
        assert [event.text for event in events if event.kind == "done"] == ["Hello"]
        assert workflow.calls[0]["message"] == "hi"
        assert "unit_entered" in [record.name for record in journal.read()]
        assert "unit_exited" in [record.name for record in journal.read()]

    @pytest.mark.asyncio
    async def test_uses_final_result_when_stream_has_no_text(self, fake_workflows_modules):
        workflow = _LocalWorkflow(events=[_StopEvent("ignored")], result={"result": "Final text"})
        bridge = LlamaAgentsBridge(workflow=workflow)

        events = []
        async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(event)

        assert [event.text for event in events if event.kind == "text_delta"] == ["Final text"]
        assert [event.text for event in events if event.kind == "done"] == ["Final text"]

    @pytest.mark.asyncio
    async def test_preserves_context_between_runs(self, fake_workflows_modules):
        workflow = _LocalWorkflow(result="ok")
        bridge = LlamaAgentsBridge(workflow=workflow)

        async for _ in bridge.invoke(AgentTurnInput.from_text("one"), _recorder()):
            pass
        first_ctx = workflow.last_handler.ctx
        async for _ in bridge.invoke(AgentTurnInput.from_text("two"), _recorder()):
            pass

        assert workflow.calls[1]["ctx"] is first_ctx

    @pytest.mark.asyncio
    async def test_cancellation_calls_handler_cancel_run(self, fake_workflows_modules):
        workflow = _LocalWorkflow(events=[_TextEvent("late")], result="late")
        bridge = LlamaAgentsBridge(workflow=workflow)
        token = CancelToken()
        token.cancel()

        events = []
        async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder(), token):
            events.append(event)

        assert workflow.last_handler.cancelled is True
        assert [event.text for event in events if event.kind == "text_delta"] == []

    @pytest.mark.asyncio
    async def test_human_input_event_pauses_and_resumes_handler(self, fake_workflows_modules):
        workflow = _HitlWorkflow()
        bridge = LlamaAgentsBridge(workflow=workflow)

        first_turn = []
        async for event in bridge.invoke(AgentTurnInput.from_text("start"), _recorder()):
            first_turn.append(event)

        assert [event.text for event in first_turn if event.kind == "text_delta"] == [
            "What is your name?"
        ]
        assert [event.text for event in first_turn if event.kind == "done"] == [
            "What is your name?"
        ]

        second_turn = []
        async for event in bridge.invoke(AgentTurnInput.from_text("Ada"), _recorder()):
            second_turn.append(event)

        assert workflow.handler.sent_events[-1].response == "Ada"
        assert [event.text for event in second_turn if event.kind == "text_delta"] == [
            "Thanks ",
            "Ada",
        ]
        assert [event.text for event in second_turn if event.kind == "done"] == ["Thanks Ada"]

    def test_apply_interruption_uses_atomic_recorder_and_delegate(self, fake_workflows_modules):
        workflow = _LocalWorkflow(result="ok")
        bridge = LlamaAgentsBridge(workflow=workflow)
        journal = InMemoryRingBuffer(capacity=1000)

        bridge.apply_interruption("part", CancellationMode.IMMEDIATE_STOP, _recorder(journal))

        assert workflow.interruption == ("part", CancellationMode.IMMEDIATE_STOP)
        names = [record.name for record in journal.read()]
        assert "state_committed" in names
        assert "cancellation_boundary" in names

    def test_constructor_requires_single_mode(self):
        with pytest.raises(BridgeInputError, match="requires"):
            LlamaAgentsBridge()
        with pytest.raises(BridgeInputError, match="not both"):
            LlamaAgentsBridge(workflow=object(), client=object(), workflow_name="wf")
        with pytest.raises(BridgeInputError, match="workflow_name"):
            LlamaAgentsBridge(client=object())


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


class TestRemoteLlamaAgentsBridge:
    @pytest.mark.asyncio
    async def test_streams_remote_workflow_client_events(self, fake_workflows_modules):
        client = _RemoteClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")

        events = []
        async for event in bridge.invoke(AgentTurnInput.from_text("Ada"), _recorder()):
            events.append(event)

        assert [event.text for event in events if event.kind == "text_delta"] == [
            "remote ",
            "text",
        ]
        assert [event.text for event in events if event.kind == "done"] == ["remote text"]
        start_event = client.run_calls[0]["start_event"]
        assert start_event.message == "Ada"

    @pytest.mark.asyncio
    async def test_remote_human_input_event_uses_send_event(self, fake_workflows_modules):
        client = _RemoteHitlClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")

        first_turn = []
        async for event in bridge.invoke(AgentTurnInput.from_text("start"), _recorder()):
            first_turn.append(event)

        assert [event.text for event in first_turn if event.kind == "text_delta"] == [
            "Remote prompt"
        ]

        second_turn = []
        async for event in bridge.invoke(AgentTurnInput.from_text("Ada"), _recorder()):
            second_turn.append(event)

        assert client.sent_events[-1][0] == "h1"
        assert client.sent_events[-1][1].response == "Ada"
        assert client.stream_calls[1]["after_sequence"] == 0
        assert [event.text for event in second_turn if event.kind == "done"] == ["Remote done"]

    @pytest.mark.asyncio
    async def test_remote_failed_handler_status_raises(self, fake_workflows_modules):
        class _FailingClient(_RemoteClient):
            async def get_handler(self, handler_id: str) -> _HandlerData:
                return _HandlerData(handler_id, result=None, status="failed", error="boom")

        client = _FailingClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")
        journal = InMemoryRingBuffer(capacity=1000)

        with pytest.raises(RuntimeError, match="failed"):
            async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder(journal)):
                pass

        names = [record.name for record in journal.read()]
        assert "framework_error" in names
        exit_records = [r for r in journal.read() if r.name == "unit_exited"]
        assert exit_records and exit_records[-1].data.get("exit_reason") == "error"

    @pytest.mark.asyncio
    async def test_remote_extracts_text_from_nested_dict_envelope(self, fake_workflows_modules):
        class _NestedClient(_RemoteClient):
            def get_workflow_events(self, handler_id: str, **kwargs: Any) -> _RemoteStream:
                self.stream_calls.append({"handler_id": handler_id, **kwargs})
                return _RemoteStream([_StopEvent("ignored")])

            async def get_handler(self, handler_id: str) -> _HandlerData:
                envelope = {"value": {"result": {"message": "Hello from envelope"}}}
                return _HandlerData(handler_id, result=envelope)

        client = _NestedClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")

        events = []
        async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(event)

        assert [event.text for event in events if event.kind == "text_delta"] == [
            "Hello from envelope"
        ]
        assert [event.text for event in events if event.kind == "done"] == ["Hello from envelope"]


class TestAutoAdapt:
    def test_auto_adapt_llama_workflow(self, fake_workflows_modules):
        from easycat.integrations.agents._factory import auto_adapt_agent

        workflow = _LocalWorkflow(result="ok")
        adapted = auto_adapt_agent(workflow)

        assert isinstance(adapted, LlamaAgentsBridge)
