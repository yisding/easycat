"""PydanticAI history safety across raced interruption teardown."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any, Self

import pytest

from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import (
    AgentTurnInput,
    CancellationMode,
    RecorderContext,
)
from easycat.integrations.agents.pydantic_ai import PydanticAIBridge
from easycat.runtime import InMemoryRingBuffer


class TextPart:
    def __init__(self, content: str) -> None:
        self.content = content


class UserPromptPart:
    def __init__(self, content: str) -> None:
        self.content = content


class ModelRequest:
    def __init__(self, *, parts: list[Any]) -> None:
        self.parts = parts


class ModelResponse:
    def __init__(self, *, parts: list[Any]) -> None:
        self.parts = parts


@pytest.fixture
def pydantic_messages(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    package = ModuleType("pydantic_ai")
    messages = ModuleType("pydantic_ai.messages")
    messages.ModelRequest = ModelRequest
    messages.ModelResponse = ModelResponse
    messages.TextPart = TextPart
    messages.UserPromptPart = UserPromptPart
    package.messages = messages
    monkeypatch.setitem(sys.modules, "pydantic_ai", package)
    monkeypatch.setitem(sys.modules, "pydantic_ai.messages", messages)
    return SimpleNamespace(
        ModelRequest=ModelRequest,
        ModelResponse=ModelResponse,
        TextPart=TextPart,
        UserPromptPart=UserPromptPart,
    )


def _recorder(*, session_id: str = "session-1") -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=InMemoryRingBuffer(capacity=100),
        artifact_store=None,
        context=RecorderContext(
            run_id="run-1",
            session_id=session_id,
            turn_id="turn-1",
        ),
    )


class TextPartDelta:
    def __init__(self, content_delta: str) -> None:
        self.content_delta = content_delta


class PartDeltaEvent:
    def __init__(self, text: str, *, index: int = 0) -> None:
        self.delta = TextPartDelta(text)
        self.index = index


class _EventStream:
    def __init__(self) -> None:
        self._yielded = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def __aiter__(self) -> _EventStream:
        return self

    async def __anext__(self) -> PartDeltaEvent:
        if self._yielded:
            raise StopAsyncIteration
        self._yielded = True
        return PartDeltaEvent("partial")


class ModelRequestNode:
    def stream(self, _ctx: object) -> _EventStream:
        return _EventStream()


class _IterRun:
    def __init__(self, messages: list[Any]) -> None:
        self._messages = messages
        self._yielded = False
        self.ctx = object()
        self.output = "partial"
        self.result = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def __aiter__(self) -> _IterRun:
        return self

    async def __anext__(self) -> ModelRequestNode:
        if self._yielded:
            raise StopAsyncIteration
        self._yielded = True
        return ModelRequestNode()

    def new_messages(self) -> list[Any]:
        return self._messages


class _IterAgent:
    name = "iter-agent"

    def __init__(self, messages: list[Any]) -> None:
        self._run = _IterRun(messages)

    def iter(self, _text: str, **_kwargs: Any) -> _IterRun:
        return self._run


class _RunStreamResult:
    def __init__(self, messages: list[Any]) -> None:
        self._messages = messages
        self.output = "partial"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def stream_text(self):
        yield "partial"

    def new_messages(self) -> list[Any]:
        return self._messages


class _RunStreamAgent:
    name = "run-stream-agent"

    def __init__(self, messages: list[Any]) -> None:
        self._result = _RunStreamResult(messages)

    def run_stream(self, _text: str, **_kwargs: Any) -> _RunStreamResult:
        return self._result


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_cls", [_IterAgent, _RunStreamAgent])
async def test_aclose_commits_current_turn_before_interruption_rewrite(
    pydantic_messages: SimpleNamespace,
    agent_cls: type[_IterAgent | _RunStreamAgent],
) -> None:
    prior_user = pydantic_messages.ModelRequest(
        parts=[pydantic_messages.UserPromptPart("previous")]
    )
    prior_response = pydantic_messages.ModelResponse(
        parts=[pydantic_messages.TextPart("previous complete")]
    )
    current_user = pydantic_messages.ModelRequest(
        parts=[pydantic_messages.UserPromptPart("current")]
    )
    current_response = pydantic_messages.ModelResponse(
        parts=[pydantic_messages.TextPart("partial")]
    )
    current_messages = [current_user, current_response]
    committed = [prior_user, prior_response, *current_messages]
    bridge = PydanticAIBridge(agent=agent_cls(current_messages))
    bridge._set_history_for_key("session-1", [prior_user, prior_response])
    recorder = _recorder()

    stream = bridge.invoke(AgentTurnInput.from_text("current"), recorder)
    first = await anext(stream)
    assert first.kind == "text_delta"
    await stream.aclose()

    bridge.apply_interruption(
        "heard",
        CancellationMode.IMMEDIATE_STOP,
        recorder=recorder,
    )

    assert prior_response.parts[0].content == "previous complete"
    assert current_response.parts[0].content == "heard..."
    assert bridge._history_for_key("session-1") == committed


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_cls", [_IterAgent, _RunStreamAgent])
async def test_completed_run_appends_sdk_new_messages_to_existing_history(
    pydantic_messages: SimpleNamespace,
    agent_cls: type[_IterAgent | _RunStreamAgent],
) -> None:
    prior_user = pydantic_messages.ModelRequest(
        parts=[pydantic_messages.UserPromptPart("previous")]
    )
    prior_response = pydantic_messages.ModelResponse(
        parts=[pydantic_messages.TextPart("previous complete")]
    )
    current_user = pydantic_messages.ModelRequest(
        parts=[pydantic_messages.UserPromptPart("current")]
    )
    current_response = pydantic_messages.ModelResponse(
        parts=[pydantic_messages.TextPart("current complete")]
    )
    bridge = PydanticAIBridge(agent=agent_cls([current_user, current_response]))
    bridge._set_history_for_key("session-1", [prior_user, prior_response])

    events = [
        event
        async for event in bridge.invoke(
            AgentTurnInput.from_text("current"),
            _recorder(),
        )
    ]

    assert events[-1].kind == "done"
    assert bridge._history_for_key("session-1") == [
        prior_user,
        prior_response,
        current_user,
        current_response,
    ]


@pytest.mark.asyncio
async def test_real_sdk_new_messages_extend_prior_history() -> None:
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
    from pydantic_ai.models.test import TestModel

    prior_user = ModelRequest(parts=[UserPromptPart(content="previous")])
    prior_response = ModelResponse(parts=[TextPart(content="previous complete")])
    bridge = PydanticAIBridge(agent=Agent(TestModel(custom_output_text="current complete")))
    bridge._set_history_for_key("session-1", [prior_user, prior_response])

    events = [
        event
        async for event in bridge.invoke(
            AgentTurnInput.from_text("current"),
            _recorder(),
        )
    ]

    history = bridge._history_for_key("session-1")
    assert events[-1].kind == "done"
    assert history[:2] == [prior_user, prior_response]
    assert any(
        isinstance(message, ModelRequest)
        and any(
            isinstance(part, UserPromptPart) and part.content == "current"
            for part in message.parts
        )
        for message in history[2:]
    )
    assert any(
        isinstance(message, ModelResponse)
        and any(
            isinstance(part, TextPart) and part.content == "current complete"
            for part in message.parts
        )
        for message in history[2:]
    )


@pytest.mark.asyncio
async def test_real_sdk_aclose_preserves_prior_turn_and_commits_current_turn() -> None:
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
    from pydantic_ai.models.test import TestModel

    prior_user = ModelRequest(parts=[UserPromptPart(content="previous")])
    prior_response = ModelResponse(parts=[TextPart(content="previous complete")])
    bridge = PydanticAIBridge(agent=Agent(TestModel(custom_output_text="current full")))
    bridge._set_history_for_key("session-1", [prior_user, prior_response])

    stream = bridge.invoke(AgentTurnInput.from_text("current"), _recorder())
    first = await anext(stream)
    assert first.kind == "text_replace"
    assert first.part_index == 0
    await stream.aclose()

    bridge.apply_interruption(
        "heard",
        CancellationMode.IMMEDIATE_STOP,
        recorder=_recorder(),
    )

    history = bridge._history_for_key("session-1")
    assert history[:2] == [prior_user, prior_response]
    assert prior_response.parts[0].content == "previous complete"
    current_response = next(
        message for message in reversed(history) if isinstance(message, ModelResponse)
    )
    current_text = next(part for part in current_response.parts if isinstance(part, TextPart))
    assert current_text.content == "heard..."


def test_interruption_synthesizes_response_at_current_user_boundary(
    pydantic_messages: SimpleNamespace,
) -> None:
    prior_response = pydantic_messages.ModelResponse(
        parts=[pydantic_messages.TextPart("previous complete")]
    )
    current_user = pydantic_messages.ModelRequest(
        parts=[pydantic_messages.UserPromptPart("current")]
    )
    bridge = PydanticAIBridge(agent=object())
    bridge._message_history = [prior_response, current_user]

    bridge.apply_interruption("wrong turn", CancellationMode.IMMEDIATE_STOP)
    bridge.replace_last_assistant_text("also wrong")

    assert prior_response.parts[0].content == "previous complete"
    current_response = bridge._message_history[-1]
    assert isinstance(current_response, pydantic_messages.ModelResponse)
    assert current_response.parts[0].content == "also wrong"
