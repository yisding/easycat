"""LlamaAgentsBridge tests using local fakes instead of provider packages."""

from __future__ import annotations

import asyncio
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
    async def test_cancellation_during_idle_stream_is_prompt(self, fake_workflows_modules):
        workflow = _BlockingWorkflow()
        handler = workflow.handler
        bridge = LlamaAgentsBridge(workflow=workflow)
        token = CancelToken()
        collected: list[Any] = []

        async def _consume() -> None:
            async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder(), token):
                collected.append(event)

        async def _cancel_when_blocked() -> None:
            await handler.streaming_blocked.wait()
            token.cancel()

        # Without racing the stream wait against cancellation this would hang
        # on the blocked step until the timeout fires.
        await asyncio.wait_for(asyncio.gather(_consume(), _cancel_when_blocked()), timeout=2.0)

        assert handler.cancelled is True
        assert [e.text for e in collected if e.kind == "text_delta"] == ["partial "]

    @pytest.mark.asyncio
    async def test_hard_task_cancel_stops_handler_and_drops_ctx(self, fake_workflows_modules):
        """Cancelling the invoke task (not just the token) must still stop the
        workflow and not preserve the interrupted Context."""
        workflow = _BlockingWorkflow()
        handler = workflow.handler
        bridge = LlamaAgentsBridge(workflow=workflow)

        async def _consume() -> None:
            async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
                pass

        task = asyncio.ensure_future(_consume())
        await asyncio.wait_for(handler.streaming_blocked.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert handler.cancelled is True
        # The interrupted Context is dropped, not carried into the next turn.
        assert bridge.snapshot_state().fields["has_context"] is False

    @pytest.mark.asyncio
    async def test_early_generator_close_cancels_running_workflow(self, fake_workflows_modules):
        """Closing invoke() mid-stream (GeneratorExit) must stop the workflow.

        A text-session interruption aclose()s the invoke generator instead of
        setting the cancel_token; without propagating that close into the
        inner stream the workflow keeps running and contaminates later turns.
        """
        workflow = _BlockingWorkflow()
        handler = workflow.handler
        bridge = LlamaAgentsBridge(workflow=workflow)

        agen = bridge.invoke(AgentTurnInput.from_text("hi"), _recorder())
        first = await agen.__anext__()
        assert first.kind == "text_delta" and first.text == "partial "

        await asyncio.wait_for(agen.aclose(), timeout=2.0)

        assert handler.cancelled is True
        assert bridge.snapshot_state().fields["has_context"] is False

    @pytest.mark.asyncio
    async def test_reset_cancels_paused_local_hitl_handler(self, fake_workflows_modules):
        """reset() after a HITL pause must cancel the paused handler/stream,
        not just drop the references and leak the waiting workflow."""
        workflow = _CancelTrackingHitlWorkflow()
        handler = workflow.handler
        bridge = LlamaAgentsBridge(workflow=workflow)

        async for _ in bridge.invoke(AgentTurnInput.from_text("start"), _recorder()):
            pass
        assert bridge.snapshot_state().fields["waiting_for_input"] is True

        bridge.reset()
        await asyncio.gather(*list(bridge._reset_cleanup_tasks))

        assert handler.cancelled is True
        assert bridge.snapshot_state().fields["waiting_for_input"] is False

    @pytest.mark.asyncio
    async def test_aclose_cancels_paused_local_hitl_handler(self, fake_workflows_modules):
        """Session.stop()/shutdown() call aclose_if_supported(self.agent), not
        reset(), so aclose() must tear down a HITL-paused local handler too --
        and await it so the workflow is released before teardown returns."""
        workflow = _CancelTrackingHitlWorkflow()
        handler = workflow.handler
        bridge = LlamaAgentsBridge(workflow=workflow)

        async for _ in bridge.invoke(AgentTurnInput.from_text("start"), _recorder()):
            pass
        assert bridge.snapshot_state().fields["waiting_for_input"] is True

        await asyncio.wait_for(bridge.aclose(), timeout=2.0)

        assert handler.cancelled is True
        assert bridge.snapshot_state().fields["waiting_for_input"] is False

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
        # The pause resumed the original live stream cursor instead of
        # opening a second stream that would replay the prompt forever.
        assert workflow.handler.stream_calls == 1

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
    async def test_remote_hitl_retry_after_send_failure(self, fake_workflows_modules):
        """A transient send_event failure must keep the bridge waiting for
        input so a retry resends the HumanResponseEvent instead of starting
        a fresh run that leaves the paused workflow stuck."""

        class _FlakySendClient(_RemoteHitlClient):
            def __init__(self) -> None:
                super().__init__()
                self.send_attempts = 0

            async def send_event(
                self, handler_id: str, event: Any, step: str | None = None
            ) -> None:
                self.send_attempts += 1
                if self.send_attempts == 1:
                    raise RuntimeError("transient network error")
                await super().send_event(handler_id, event, step=step)

        client = _FlakySendClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")

        async for _ in bridge.invoke(AgentTurnInput.from_text("start"), _recorder()):
            pass

        with pytest.raises(RuntimeError, match="transient network error"):
            async for _ in bridge.invoke(AgentTurnInput.from_text("Ada"), _recorder()):
                pass

        retry_turn = []
        async for event in bridge.invoke(AgentTurnInput.from_text("Ada"), _recorder()):
            retry_turn.append(event)

        # Only the initial start spun up a run -- the retry resent the
        # human response to the same paused handler rather than launching
        # a second run_workflow_nowait().
        assert len(client.run_calls) == 1
        assert client.sent_events[-1][0] == "h1"
        assert client.sent_events[-1][1].response == "Ada"
        assert [event.text for event in retry_turn if event.kind == "done"] == ["Remote done"]

    @pytest.mark.asyncio
    async def test_remote_cancellation_during_idle_stream_is_prompt(self, fake_workflows_modules):
        stream = _BlockingRemoteStream()

        class _BlockingRemoteClient(_RemoteClient):
            def get_workflow_events(self, handler_id: str, **kwargs: Any) -> Any:
                self.stream_calls.append({"handler_id": handler_id, **kwargs})
                return stream

        client = _BlockingRemoteClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")
        token = CancelToken()
        collected: list[Any] = []

        async def _consume() -> None:
            async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder(), token):
                collected.append(event)

        async def _cancel_when_blocked() -> None:
            await stream.streaming_blocked.wait()
            token.cancel()

        await asyncio.wait_for(asyncio.gather(_consume(), _cancel_when_blocked()), timeout=2.0)

        assert client.cancelled == ["h1"]
        assert stream.closed is True
        assert [e.text for e in collected if e.kind == "text_delta"] == ["remote partial"]

    @pytest.mark.asyncio
    async def test_remote_hard_task_cancel_calls_cancel_handler(self, fake_workflows_modules):
        """Cancelling the invoke task (not just the token) must still stop the
        remote handler so it does not keep running server-side."""
        stream = _BlockingRemoteStream()

        class _BlockingRemoteClient(_RemoteClient):
            def get_workflow_events(self, handler_id: str, **kwargs: Any) -> Any:
                self.stream_calls.append({"handler_id": handler_id, **kwargs})
                return stream

        client = _BlockingRemoteClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")

        async def _consume() -> None:
            async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
                pass

        task = asyncio.ensure_future(_consume())
        await asyncio.wait_for(stream.streaming_blocked.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert client.cancelled == ["h1"]
        assert stream.closed is True

    @pytest.mark.asyncio
    async def test_remote_cancelled_handler_continues_next_turn(self, fake_workflows_modules):
        """A barge-in must preserve the server-side handler reference.

        ``cancel_handler`` keeps the handler persisted (purge defaults to
        False), so the next default (preserve_context) turn continues from it
        via ``handler_id`` -- the only server-side state reference, since the
        real ``llama-agents-client`` ``HandlerData`` model exposes no context
        field. Dropping it would silently lose all conversation state after
        every barge-in.
        """
        first_stream = _BlockingRemoteStream()

        class _CancelThenResumeClient(_RemoteClient):
            def get_workflow_events(self, handler_id: str, **kwargs: Any) -> Any:
                self.stream_calls.append({"handler_id": handler_id, **kwargs})
                if len(self.stream_calls) == 1:
                    return first_stream
                return _RemoteStream([_TextEvent("second turn"), _StopEvent("done")])

        client = _CancelThenResumeClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")
        token = CancelToken()

        async def _consume() -> None:
            async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder(), token):
                pass

        async def _cancel_when_blocked() -> None:
            await first_stream.streaming_blocked.wait()
            token.cancel()

        await asyncio.wait_for(asyncio.gather(_consume(), _cancel_when_blocked()), timeout=2.0)
        assert client.cancelled == ["h1"]

        async for _ in bridge.invoke(AgentTurnInput.from_text("again"), _recorder()):
            pass

        # The next turn continues from the preserved (terminal but still
        # persisted) handler instead of starting a fresh one and losing all
        # workflow state. The event cursor is carried forward -- exactly like
        # the completed-handler preserve_context path -- so the resumed
        # handler's accumulating event log is not replayed.
        assert client.run_calls[1]["handler_id"] == "h1"
        assert client.stream_calls[1]["after_sequence"] == 0

    @pytest.mark.asyncio
    async def test_remote_cancellation_advances_cursor_past_dropped_event(
        self, fake_workflows_modules
    ):
        """A barge-in that drops an event must still advance the cursor.

        When the stream's ``last_sequence`` has already moved past an event
        that the cancellation race never delivered to the loop body, the next
        preserve_context turn must resume *after* that dropped interrupted
        delta. Otherwise the stale cursor replays it as fresh speech.
        """
        first_stream = _AdvanceThenBlockStream()

        class _CancelThenResumeClient(_RemoteClient):
            def get_workflow_events(self, handler_id: str, **kwargs: Any) -> Any:
                self.stream_calls.append({"handler_id": handler_id, **kwargs})
                if len(self.stream_calls) == 1:
                    return first_stream
                return _RemoteStream([_TextEvent("second turn"), _StopEvent("done")])

        client = _CancelThenResumeClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")
        token = CancelToken()
        first_turn: list[Any] = []

        async def _consume() -> None:
            async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder(), token):
                first_turn.append(event)

        async def _cancel_when_blocked() -> None:
            await first_stream.first_delivered.wait()
            token.cancel()

        await asyncio.wait_for(asyncio.gather(_consume(), _cancel_when_blocked()), timeout=2.0)

        # Only the delivered event 0 was spoken; the undelivered event 1 was
        # dropped by the cancellation race.
        assert [e.text for e in first_turn if e.kind == "text_delta"] == ["first delta"]
        assert client.cancelled == ["h1"]

        second_turn: list[Any] = []
        async for event in bridge.invoke(AgentTurnInput.from_text("again"), _recorder()):
            second_turn.append(event)

        # The cursor advanced to the stream's last_sequence (1) even though the
        # loop body never ran for event 1, so the resumed handler streams from
        # after the dropped delta instead of replaying it.
        assert client.run_calls[1]["handler_id"] == "h1"
        assert client.stream_calls[1]["after_sequence"] == 1
        assert "interrupted second delta" not in [
            e.text for e in second_turn if e.kind == "text_delta"
        ]

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

    @pytest.mark.asyncio
    async def test_remote_preserve_context_carries_event_cursor(self, fake_workflows_modules):
        """A preserved second turn resumes after the first turn's events.

        With preserve_context the handler (and its server-side event log)
        survives into the next turn, so streaming from -1 again would replay
        the first turn before the new response.
        """

        class _PreservingClient(_RemoteClient):
            async def run_workflow_nowait(self, workflow_name: str, **kwargs: Any) -> _HandlerData:
                self.run_calls.append({"workflow_name": workflow_name, **kwargs})
                return _HandlerData("h1")

            def get_workflow_events(self, handler_id: str, **kwargs: Any) -> _RemoteStream:
                self.stream_calls.append({"handler_id": handler_id, **kwargs})
                n = len(self.stream_calls)
                return _RemoteStream([_TextEvent(f"turn {n}"), _StopEvent(f"r{n}")])

            async def get_handler(self, handler_id: str) -> _HandlerData:
                return _HandlerData(handler_id, result="ok", context={"saved": True})

        client = _PreservingClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")

        async for _ in bridge.invoke(AgentTurnInput.from_text("one"), _recorder()):
            pass
        async for _ in bridge.invoke(AgentTurnInput.from_text("two"), _recorder()):
            pass

        # First turn streams from the beginning; the preserved second turn
        # resumes after the first turn's last event instead of replaying it.
        assert client.stream_calls[0]["after_sequence"] == -1
        assert client.stream_calls[1]["after_sequence"] == 1
        # The handler is reused (preserve_context), not recreated.
        assert client.run_calls[1]["handler_id"] == "h1"

    @pytest.mark.asyncio
    async def test_remote_hitl_pause_closes_event_stream(self, fake_workflows_modules):
        """A HITL pause must close the SSE stream, not leak it until cancel."""
        streams: list[_RemoteStream] = []

        class _PausingClient(_RemoteClient):
            def get_workflow_events(self, handler_id: str, **kwargs: Any) -> _RemoteStream:
                self.stream_calls.append({"handler_id": handler_id, **kwargs})
                stream = _RemoteStream([_InputRequiredEvent(prefix="name?")])
                streams.append(stream)
                return stream

        client = _PausingClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")

        async for _ in bridge.invoke(AgentTurnInput.from_text("start"), _recorder()):
            pass

        assert streams and streams[0].closed is True

    @pytest.mark.asyncio
    async def test_remote_opaque_stop_subclass_envelope_is_terminal(self, fake_workflows_modules):
        """A server-only StopEvent subclass envelope must not be re-streamed.

        ``load_event()`` fails (the subclass isn't importable client-side) so
        the bridge keeps the envelope; it advertises StopEvent via ``types``
        and must be treated as terminal instead of having its result appended
        as a duplicate text delta after the streamed progress.
        """

        class _OpaqueStopEnvelope:
            type = "acme.CustomStopEvent"
            types = ["workflows.events.StopEvent", "workflows.events.Event"]
            value = {"result": "the answer"}
            qualified_name = "acme.CustomStopEvent"

            def load_event(self) -> Any:
                raise RuntimeError("CustomStopEvent not importable on client")

        class _OpaqueClient(_RemoteClient):
            def get_workflow_events(self, handler_id: str, **kwargs: Any) -> Any:
                self.stream_calls.append({"handler_id": handler_id, **kwargs})
                return _RawStream(
                    [_RemoteEnvelope(_TextEvent("the answer")), _OpaqueStopEnvelope()]
                )

        client = _OpaqueClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")

        events = []
        async for event in bridge.invoke(AgentTurnInput.from_text("q"), _recorder()):
            events.append(event)

        assert [e.text for e in events if e.kind == "text_delta"] == ["the answer"]
        assert [e.text for e in events if e.kind == "done"] == ["the answer"]

    @pytest.mark.asyncio
    async def test_remote_empty_result_envelope_is_not_spoken(self, fake_workflows_modules):
        """A silent StopEvent() result must not leak envelope repr to TTS."""

        class _EmptyEnvelope:
            type = "StopEvent"
            types = ["workflows.events.StopEvent"]
            value: dict[str, Any] = {}
            qualified_name = "workflows.events.StopEvent"

            def load_event(self) -> Any:
                return _StopEvent()

        class _SilentClient(_RemoteClient):
            def get_workflow_events(self, handler_id: str, **kwargs: Any) -> _RemoteStream:
                self.stream_calls.append({"handler_id": handler_id, **kwargs})
                return _RemoteStream([_StopEvent()])

            async def get_handler(self, handler_id: str) -> _HandlerData:
                return _HandlerData(handler_id, result=_EmptyEnvelope())

        client = _SilentClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")

        events = []
        async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(event)

        assert [e.text for e in events if e.kind == "text_delta"] == []
        assert [e.text for e in events if e.kind == "done"] == [""]

    @pytest.mark.asyncio
    async def test_remote_opaque_input_required_subclass_is_hitl_pause(
        self, fake_workflows_modules
    ):
        """A server-only InputRequiredEvent subclass envelope must pause.

        ``load_event()`` fails (the subclass isn't importable client-side) so
        the bridge keeps the envelope; its ``type`` is the subclass name but
        ``types`` advertises InputRequiredEvent. The bridge must still treat
        it as a HITL prompt and send the next user turn back as a
        HumanResponseEvent instead of starting a fresh workflow run.
        """

        class _OpaqueInputEnvelope:
            type = "acme.CustomInputRequiredEvent"
            types = ["workflows.events.InputRequiredEvent", "workflows.events.Event"]
            value = {"prefix": "Remote subclass prompt"}
            qualified_name = "acme.CustomInputRequiredEvent"

            def load_event(self) -> Any:
                raise RuntimeError("CustomInputRequiredEvent not importable on client")

        class _OpaqueInputClient(_RemoteClient):
            def get_workflow_events(self, handler_id: str, **kwargs: Any) -> Any:
                self.stream_calls.append({"handler_id": handler_id, **kwargs})
                if len(self.stream_calls) == 1:
                    return _RawStream([_OpaqueInputEnvelope()])
                return _RemoteStream(
                    [_TextEvent("Remote "), _TextEvent("done"), _StopEvent("done")]
                )

        client = _OpaqueInputClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")

        first_turn = []
        async for event in bridge.invoke(AgentTurnInput.from_text("start"), _recorder()):
            first_turn.append(event)

        assert [e.text for e in first_turn if e.kind == "text_delta"] == ["Remote subclass prompt"]
        # The prompt paused the run; no new workflow was started for it.
        assert len(client.run_calls) == 1

        second_turn = []
        async for event in bridge.invoke(AgentTurnInput.from_text("Ada"), _recorder()):
            second_turn.append(event)

        # The pause resumed the same handler with a HumanResponseEvent
        # instead of running the workflow again.
        assert len(client.run_calls) == 1
        assert client.sent_events[-1][0] == "h1"
        assert client.sent_events[-1][1].response == "Ada"
        assert [e.text for e in second_turn if e.kind == "done"] == ["Remote done"]

    @pytest.mark.asyncio
    async def test_remote_result_envelope_unwrapped_for_structured_output(
        self, fake_workflows_modules
    ):
        """structured_output must be StopEvent.result, not the envelope.

        The real WorkflowClient stores the final StopEvent wrapped in an
        EventEnvelope; local mode exposes the StopEvent's raw ``result``.
        Remote mode must match by unwrapping the envelope rather than
        leaking serialization metadata into structured_output.
        """

        class _StructuredClient(_RemoteClient):
            def get_workflow_events(self, handler_id: str, **kwargs: Any) -> _RemoteStream:
                self.stream_calls.append({"handler_id": handler_id, **kwargs})
                return _RemoteStream([_StopEvent("ignored")])

            async def get_handler(self, handler_id: str) -> _HandlerData:
                envelope = _RemoteEnvelope(_StopEvent(result={"score": 0.9}))
                return _HandlerData(handler_id, result=envelope)

        client = _StructuredClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")

        done = [
            event
            async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder())
            if event.kind == "done"
        ]

        assert len(done) == 1
        assert done[0].structured_output == {"score": 0.9}

    @pytest.mark.asyncio
    async def test_remote_opaque_stop_subclass_result_unwrapped(self, fake_workflows_modules):
        """structured_output must be the workflow result for an opaque
        StopEvent subclass too.

        ``load_event()`` fails (the subclass is only importable on the
        workflow server) so the envelope is kept; ``_is_stop_event()`` still
        matches it via ``types``. The real return value lives in the
        envelope's serialized ``value['result']`` -- it must be dug out
        instead of leaking the envelope's serialization metadata.
        """

        class _OpaqueStopEnvelope:
            type = "acme.CustomStopEvent"
            types = ["workflows.events.StopEvent", "workflows.events.Event"]
            value = {"result": {"score": 0.9}}
            qualified_name = "acme.CustomStopEvent"

            def load_event(self) -> Any:
                raise RuntimeError("CustomStopEvent not importable on client")

        class _OpaqueResultClient(_RemoteClient):
            def get_workflow_events(self, handler_id: str, **kwargs: Any) -> _RemoteStream:
                self.stream_calls.append({"handler_id": handler_id, **kwargs})
                return _RemoteStream([_StopEvent("ignored")])

            async def get_handler(self, handler_id: str) -> _HandlerData:
                return _HandlerData(handler_id, result=_OpaqueStopEnvelope())

        client = _OpaqueResultClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")

        done = [
            event
            async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder())
            if event.kind == "done"
        ]

        assert len(done) == 1
        assert done[0].structured_output == {"score": 0.9}

    @pytest.mark.asyncio
    async def test_remote_early_generator_close_cancels_handler(self, fake_workflows_modules):
        """Closing invoke() mid-stream (GeneratorExit) must cancel the remote
        handler so the workflow does not keep running server-side."""
        stream = _BlockingRemoteStream()

        class _BlockingRemoteClient(_RemoteClient):
            def get_workflow_events(self, handler_id: str, **kwargs: Any) -> Any:
                self.stream_calls.append({"handler_id": handler_id, **kwargs})
                return stream

        client = _BlockingRemoteClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")

        agen = bridge.invoke(AgentTurnInput.from_text("hi"), _recorder())
        first = await agen.__anext__()
        assert first.kind == "text_delta" and first.text == "remote partial"

        await asyncio.wait_for(agen.aclose(), timeout=2.0)

        assert client.cancelled == ["h1"]
        assert stream.closed is True

    @pytest.mark.asyncio
    async def test_reset_cancels_paused_remote_hitl_handler(self, fake_workflows_modules):
        """reset() after a remote HITL pause must cancel the server-side
        handler, not just drop the handler id and leak the paused workflow."""
        client = _RemoteHitlClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")

        async for _ in bridge.invoke(AgentTurnInput.from_text("start"), _recorder()):
            pass
        assert bridge.snapshot_state().fields["waiting_for_input"] is True

        bridge.reset()
        await asyncio.gather(*list(bridge._reset_cleanup_tasks))

        assert client.cancelled == ["h1"]
        assert bridge.snapshot_state().fields["waiting_for_input"] is False

    @pytest.mark.asyncio
    async def test_aclose_cancels_paused_remote_hitl_handler(self, fake_workflows_modules):
        """aclose() (the session stop/shutdown path) must cancel the paused
        server-side handler too, not just reset() between turns."""
        client = _RemoteHitlClient()
        bridge = LlamaAgentsBridge(client=client, workflow_name="greet")

        async for _ in bridge.invoke(AgentTurnInput.from_text("start"), _recorder()):
            pass
        assert bridge.snapshot_state().fields["waiting_for_input"] is True

        await asyncio.wait_for(bridge.aclose(), timeout=2.0)

        assert client.cancelled == ["h1"]
        assert bridge.snapshot_state().fields["waiting_for_input"] is False


class TestAutoAdapt:
    def test_auto_adapt_llama_workflow(self, fake_workflows_modules):
        from easycat.integrations.agents._factory import auto_adapt_agent

        workflow = _LocalWorkflow(result="ok")
        adapted = auto_adapt_agent(workflow)

        assert isinstance(adapted, LlamaAgentsBridge)
