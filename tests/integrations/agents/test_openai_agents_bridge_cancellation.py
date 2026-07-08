"""OpenAI Agents bridge barge-in cancellation tests.

Regression coverage for the bug where ``invoke()`` broke out of
``result.stream_events()`` on a cancel-token barge-in without ever calling
``result.cancel()``. ``Runner.run_streamed`` drives the agent loop in a
background task, so abandoning the stream lets the run finish: post-cancel
tool side-effects fire and ``to_input_list()`` is snapshotted from a
still-mutating run. These tests assert the bridge now cancels the run with the
right mode, no post-cancel side-effects fire, and the captured input-list is
stable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from easycat.cancel import CancelToken
from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import AgentTurnInput, RecorderContext
from easycat.integrations.agents.openai_agents import OpenAIAgentsBridge
from easycat.runtime import InMemoryRingBuffer


def _recorder(journal: InMemoryRingBuffer | None = None) -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=journal or InMemoryRingBuffer(capacity=1000),
        artifact_store=None,
        context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
    )


class _Agent:
    def __init__(self, name: str = "Agent") -> None:
        self.name = name
        self.mcp_servers: list[Any] = []


class _CancellableRunResult:
    """Fake ``RunResultStreaming`` that records cancel() and models the SDK.

    ``stream_events()`` yields the supplied events; entries that are callables
    are barge-in hooks (they fire the cancel token instead of being yielded).
    ``cancel(mode)`` records the mode. ``to_input_list()`` models the SDK's
    ``finally`` awaiting the background run to completion: if the run was never
    cancelled, the still-running loop fires post-cancel tool side-effects and
    mutates the snapshot; once cancelled it returns the settled list unchanged.
    """

    def __init__(
        self,
        *,
        last_agent: Any,
        events: list[Any],
        settled_input: list[Any],
        last_response_id: str | None = "resp-1",
        final_output: Any = "final",
    ) -> None:
        self.last_agent = last_agent
        self.last_response_id = last_response_id
        self.final_output = final_output
        self._events = events
        self._settled_input = settled_input
        self.cancel_calls: list[str] = []
        self.side_effects: list[str] = []

    def cancel(self, mode: str = "immediate") -> None:
        self.cancel_calls.append(mode)

    async def stream_events(self) -> AsyncIterator[Any]:
        for event in self._events:
            if callable(event):
                event()  # barge-in hook: fire the cancel token
                continue
            yield event

    def to_input_list(self) -> list[Any]:
        if not self.cancel_calls:
            # Un-cancelled: the SDK's finally awaits run_loop_task, so the
            # abandoned run keeps executing tools and mutates the snapshot.
            self.side_effects.append("post-cancel-tool")
            return [*self._settled_input, {"role": "tool", "content": "post-cancel"}]
        return list(self._settled_input)


class _FakeRunner:
    def __init__(self, result: _CancellableRunResult) -> None:
        self._result = result

    def run_streamed(self, agent: Any, input_data: Any, **kwargs: Any) -> _CancellableRunResult:
        return self._result


def _text_event(delta: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="raw_response_event",
        data=SimpleNamespace(type="response.output_text.delta", delta=delta),
    )


def _tool_call_event(name: str, call_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="run_item_stream_event",
        item=SimpleNamespace(
            type="tool_call_item",
            raw_item=SimpleNamespace(name=name, call_id=call_id),
        ),
    )


def _tool_output_event(call_id: str, output: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="run_item_stream_event",
        item=SimpleNamespace(
            type="tool_call_output_item",
            raw_item={"call_id": call_id, "output": output, "type": "function_call_output"},
            output=output,
        ),
    )


def _barge_in(token: CancelToken) -> Callable[[], None]:
    return lambda: token.cancel()


@pytest.mark.asyncio
async def test_barge_in_with_pending_tool_cancels_after_turn(monkeypatch):
    import easycat.integrations.agents.openai_agents as openai_agents_module

    agent = _Agent()
    token = CancelToken()
    settled = [
        {"role": "user", "content": "look it up"},
        {"role": "assistant", "content": "checking"},
    ]
    settling: list[str] = []
    result = _CancellableRunResult(
        last_agent=agent,
        settled_input=settled,
        events=[
            _tool_call_event("lookup", "call-1"),  # populates pending_tool_calls
            _barge_in(token),  # user cuts in while the tool is in flight
            _tool_output_event("call-1", "ok"),  # in-flight tool drains
            _text_event("post-cancel"),  # SDK settles the turn after the tool
            lambda: settling.append("stream-drained"),  # only reached if fully drained
        ],
    )
    monkeypatch.setattr(openai_agents_module, "Runner", _FakeRunner(result))
    bridge = OpenAIAgentsBridge(agent)

    kinds = [
        event.kind
        async for event in bridge.invoke(
            AgentTurnInput.from_text("look it up"), _recorder(), token
        )
    ]

    # (a) the run was cancelled with after_turn to drain the in-flight tool.
    assert result.cancel_calls == ["after_turn"]
    # (b) no post-cancel tool side-effects fired.
    assert result.side_effects == []
    # (c) the captured input-list is the settled snapshot, not a mutating run.
    assert bridge._message_history == settled
    # The in-flight tool still drained through to the consumer.
    assert "tool_started" in kinds
    assert "tool_result" in kinds
    # ``after_turn`` requires consuming ``stream_events()`` to completion so
    # the SDK can settle session state: the stream was fully drained, and the
    # post-cancel text delta was swallowed rather than surfaced.
    assert settling == ["stream-drained"]
    assert "text_delta" not in kinds


@pytest.mark.asyncio
async def test_barge_in_mid_text_cancels_immediate(monkeypatch):
    import easycat.integrations.agents.openai_agents as openai_agents_module

    agent = _Agent()
    token = CancelToken()
    settled = [
        {"role": "user", "content": "tell me"},
        {"role": "assistant", "content": "Hel"},
    ]
    result = _CancellableRunResult(
        last_agent=agent,
        settled_input=settled,
        events=[
            _text_event("Hel"),  # partial answer streamed
            _barge_in(token),  # user cuts in mid-sentence, no tool pending
            _text_event("lo there"),  # would keep streaming without a cancel
        ],
    )
    monkeypatch.setattr(openai_agents_module, "Runner", _FakeRunner(result))
    bridge = OpenAIAgentsBridge(agent)

    texts = [
        event.text
        async for event in bridge.invoke(AgentTurnInput.from_text("tell me"), _recorder(), token)
        if event.kind == "text_delta"
    ]

    # (a) no pending tool -> cancel immediately.
    assert result.cancel_calls == ["immediate"]
    # (b) no post-cancel side-effects.
    assert result.side_effects == []
    # (c) the captured input-list is stable.
    assert bridge._message_history == settled
    # Streaming stopped at the barge-in: the post-cancel delta never surfaced.
    assert texts == ["Hel"]


@pytest.mark.asyncio
async def test_generator_close_strips_dangling_tool_calls(monkeypatch):
    """A hard aclose() mid-tool-call must not poison the captured history.

    The snapshot can contain a ``function_call`` whose output never arrived;
    replaying that input list is rejected by the Responses API, so the bridge
    drops the unmatched call."""
    import easycat.integrations.agents.openai_agents as openai_agents_module

    agent = _Agent()
    settled = [
        {"role": "user", "content": "look it up"},
        {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"},
        {"type": "function_call", "call_id": "call-2", "name": "other", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call-2", "output": "done"},
    ]
    result = _CancellableRunResult(
        last_agent=agent,
        settled_input=settled,
        events=[_tool_call_event("lookup", "call-1"), _text_event("x")],
    )
    monkeypatch.setattr(openai_agents_module, "Runner", _FakeRunner(result))
    bridge = OpenAIAgentsBridge(agent)

    agen = bridge.invoke(AgentTurnInput.from_text("look it up"), _recorder())
    first = await agen.__anext__()
    assert first.kind == "tool_started"
    await agen.aclose()

    assert result.cancel_calls == ["immediate"]
    # call-1 never got an output -> dropped; the completed call-2 pair stays.
    assert bridge._message_history == [settled[0], settled[2], settled[3]]


@pytest.mark.asyncio
async def test_generator_close_survives_cancel_error(monkeypatch):
    """An SDK error from cancel() must not supersede the GeneratorExit —
    that would turn a clean barge-in aclose() into
    RuntimeError('async generator ignored GeneratorExit')."""
    import easycat.integrations.agents.openai_agents as openai_agents_module

    agent = _Agent()

    class _RaisingCancelResult(_CancellableRunResult):
        def cancel(self, mode: str = "immediate") -> None:
            raise RuntimeError("run already finalized")

    result = _RaisingCancelResult(
        last_agent=agent,
        settled_input=[{"role": "user", "content": "hi"}],
        events=[_text_event("Hel"), _text_event("lo")],
    )
    monkeypatch.setattr(openai_agents_module, "Runner", _FakeRunner(result))
    bridge = OpenAIAgentsBridge(agent)

    agen = bridge.invoke(AgentTurnInput.from_text("hi"), _recorder())
    first = await agen.__anext__()
    assert first.kind == "text_delta"
    await agen.aclose()  # must not raise


@pytest.mark.asyncio
async def test_generator_close_cancels_run(monkeypatch):
    import easycat.integrations.agents.openai_agents as openai_agents_module

    agent = _Agent()
    settled = [{"role": "user", "content": "hi"}]
    result = _CancellableRunResult(
        last_agent=agent,
        settled_input=settled,
        events=[_text_event("Hel"), _text_event("lo")],
    )
    monkeypatch.setattr(openai_agents_module, "Runner", _FakeRunner(result))
    bridge = OpenAIAgentsBridge(agent)

    agen = bridge.invoke(AgentTurnInput.from_text("hi"), _recorder())
    first = await agen.__anext__()
    assert first.kind == "text_delta"
    # An external aclose() (text-session barge-in) must cancel the background run.
    await agen.aclose()

    assert result.cancel_calls == ["immediate"]
    assert result.side_effects == []
