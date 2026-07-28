"""PydanticAI history safety across cancellation and response-less turns."""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from easycat.integrations.agents._recorder import JournalAgentRecorder
from easycat.integrations.agents.base import AgentTurnInput, CancellationMode, RecorderContext
from easycat.integrations.agents.pydantic_ai import PydanticAIBridge
from easycat.runtime import InMemoryRingBuffer


@pytest.fixture
def messages(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    class TextPart:
        def __init__(self, content: str) -> None:
            self.content = content

    class UserPromptPart:
        def __init__(self, content: str) -> None:
            self.content = content

    class ModelRequest:
        def __init__(self, parts: list[Any]) -> None:
            self.parts = parts

    class ModelResponse:
        def __init__(self, parts: list[Any]) -> None:
            self.parts = parts

    package = ModuleType("pydantic_ai")
    package.__path__ = []  # type: ignore[attr-defined]
    module = ModuleType("pydantic_ai.messages")
    module.TextPart = TextPart  # type: ignore[attr-defined]
    module.UserPromptPart = UserPromptPart  # type: ignore[attr-defined]
    module.ModelRequest = ModelRequest  # type: ignore[attr-defined]
    module.ModelResponse = ModelResponse  # type: ignore[attr-defined]
    package.messages = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pydantic_ai", package)
    monkeypatch.setitem(sys.modules, "pydantic_ai.messages", module)
    return SimpleNamespace(
        TextPart=TextPart,
        UserPromptPart=UserPromptPart,
        ModelRequest=ModelRequest,
        ModelResponse=ModelResponse,
    )


class TextPartDelta:
    def __init__(self, content_delta: str) -> None:
        self.content_delta = content_delta


class PartDeltaEvent:
    def __init__(self, delta: Any) -> None:
        self.delta = delta


class _NodeStream:
    async def __aenter__(self) -> _NodeStream:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def __aiter__(self):
        return self._events()

    async def _events(self):
        yield PartDeltaEvent(TextPartDelta("partial reply"))
        await asyncio.Event().wait()


class ModelRequestNode:
    def stream(self, ctx: Any) -> _NodeStream:
        return _NodeStream()


class _AgentRun:
    output = None
    result = None
    ctx = object()

    def __init__(self, current_messages: list[Any]) -> None:
        self._current_messages = current_messages
        self._yielded_node = False

    async def __aenter__(self) -> _AgentRun:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def __aiter__(self) -> _AgentRun:
        return self

    async def __anext__(self) -> Any:
        if not self._yielded_node:
            self._yielded_node = True
            return ModelRequestNode()
        await asyncio.Event().wait()
        raise StopAsyncIteration

    def new_messages(self) -> list[Any]:
        return self._current_messages


class _IterAgent:
    name = "iter-agent"

    def __init__(self, current_messages: list[Any]) -> None:
        self._run = _AgentRun(current_messages)

    def iter(self, text: str, **kwargs: Any) -> _AgentRun:
        return self._run


class _RunStreamResult:
    output = None
    result = None

    def __init__(self, current_messages: list[Any]) -> None:
        self._current_messages = current_messages

    async def __aenter__(self) -> _RunStreamResult:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def stream_text(self):
        yield "partial reply"
        await asyncio.Event().wait()

    def new_messages(self) -> list[Any]:
        return self._current_messages


class _RunStreamAgent:
    name = "run-stream-agent"

    def __init__(self, current_messages: list[Any]) -> None:
        self._result = _RunStreamResult(current_messages)

    def run_stream(self, text: str, **kwargs: Any) -> _RunStreamResult:
        return self._result


def _recorder() -> JournalAgentRecorder:
    return JournalAgentRecorder(
        journal=InMemoryRingBuffer(capacity=100),
        artifact_store=None,
        context=RecorderContext(run_id="run", session_id="session", turn_id="turn"),
    )


def _turn_messages(messages: SimpleNamespace) -> tuple[list[Any], Any, Any]:
    previous = messages.ModelResponse([messages.TextPart("previous reply")])
    current = messages.ModelResponse([messages.TextPart("partial reply")])
    turn = [
        messages.ModelRequest([messages.UserPromptPart("current question")]),
        current,
    ]
    return turn, previous, current


@pytest.mark.parametrize("agent_type", [_IterAgent, _RunStreamAgent])
@pytest.mark.asyncio
async def test_aclose_commits_current_turn_before_interruption(
    messages: SimpleNamespace, agent_type: type[Any]
) -> None:
    turn, previous, current = _turn_messages(messages)
    bridge = PydanticAIBridge(agent=agent_type(turn))
    bridge._message_history = [
        messages.ModelRequest([messages.UserPromptPart("previous question")]),
        previous,
    ]

    stream = bridge.invoke(AgentTurnInput.from_text("current question"), _recorder())
    event = await anext(stream)
    assert event.kind == "text_delta"
    assert event.text == "partial reply"
    await stream.aclose()

    assert bridge._history_for_key("session") == turn
    bridge.apply_interruption(
        "partial reply",
        CancellationMode.IMMEDIATE_STOP,
        recorder=_recorder(),
    )
    assert current.parts[0].content == "partial reply..."
    assert previous.parts[0].content == "previous reply"


def test_rewrites_stop_at_latest_user_request(messages: SimpleNamespace) -> None:
    previous = messages.ModelResponse([messages.TextPart("previous reply")])
    bridge = PydanticAIBridge(agent=object())
    bridge._message_history = [
        messages.ModelRequest([messages.UserPromptPart("previous question")]),
        previous,
        messages.ModelRequest([messages.UserPromptPart("current question")]),
    ]

    bridge.apply_interruption("current partial", CancellationMode.IMMEDIATE_STOP)
    bridge.replace_last_assistant_text("current cleaned")

    assert previous.parts[0].content == "previous reply"
