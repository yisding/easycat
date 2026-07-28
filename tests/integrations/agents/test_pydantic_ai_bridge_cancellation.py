"""PydanticAI history safety across raced interruption teardown."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

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
    def __init__(self, text: str) -> None:
        self.delta = TextPartDelta(text)


class _EventStream:
    def __init__(self) -> None:
        self._yielded = False

    async def __aenter__(self) -> _EventStream:
        return self

    async def __aexit__(self, *_args: Any) -> None:
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

    async def __aenter__(self) -> _IterRun:
        return self

    async def __aexit__(self, *_args: Any) -> None:
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

    async def __aenter__(self) -> _RunStreamResult:
        return self

    async def __aexit__(self, *_args: Any) -> None:
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
    agent_cls: type[_IterAgent] | type[_RunStreamAgent],
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
    committed = [prior_user, prior_response, current_user, current_response]
    bridge = PydanticAIBridge(agent=agent_cls(committed))
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


def test_interruption_and_postprocessing_stop_at_current_user_boundary(
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
