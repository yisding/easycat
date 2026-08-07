"""Keep Chapter 14's interruption lesson aligned with the live bridge."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType, SimpleNamespace, TracebackType
from typing import Self

import pytest

from easycat import create_text_session
from easycat.cancel import CancelToken
from easycat.integrations.agents.base import (
    NULL_RECORDER,
    AgentTurnInput,
    CancellationMode,
    ShallowModeInterruptionError,
)
from easycat.integrations.agents.generic_workflow import GenericWorkflowBridge

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "teaching" / "14-bring-your-own-agent"


def _load_main_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = CHAPTER / "main.py"
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=object))
    spec = importlib.util.spec_from_file_location("teaching_ch14_interruption", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_deep_workflow_rewrites_private_history_to_delivered_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chapter = _load_main_module(monkeypatch)

    class ResponseStream:
        def __init__(self) -> None:
            self.closed = False
            self._texts = iter(("Hello ", "from text the caller never heard"))

        def __aiter__(self) -> ResponseStream:
            return self

        async def __anext__(self) -> SimpleNamespace:
            try:
                text = next(self._texts)
            except StopIteration as exc:
                raise StopAsyncIteration from exc
            return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            self.closed = True

    class Completions:
        def __init__(self) -> None:
            self.stream = ResponseStream()

        async def create(self, **_kwargs: object) -> ResponseStream:
            return self.stream

    completions = Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    workflow = chapter.MyWorkflow(client, chapter.SessionActions())
    bridge = GenericWorkflowBridge(workflow)
    token = CancelToken()
    stream = bridge.invoke(
        AgentTurnInput.from_text("hello"),
        NULL_RECORDER,
        token,
    )

    assert bridge.deep_mode
    first = await anext(stream)
    assert first.kind == "text_delta"
    assert first.text == "Hello "
    await stream.aclose()
    assert completions.stream.closed
    assert workflow._history[-1] == {"role": "assistant", "content": "Hello "}

    bridge.apply_interruption("Hello", CancellationMode.IMMEDIATE_STOP)
    assert workflow._history[-1] == {"role": "assistant", "content": "Hello..."}

    workflow.replace_last_assistant_text("Hello")
    assert workflow._history[-1] == {"role": "assistant", "content": "Hello"}

    cancelled_before_output = CancelToken()
    cancelled_before_output.cancel()
    empty_stream = workflow.on_user_turn(
        "second turn",
        recorder=NULL_RECORDER,
        cancel_token=cancelled_before_output,
    )
    with pytest.raises(StopAsyncIteration):
        await anext(empty_stream)
    workflow.apply_interruption("", CancellationMode.IMMEDIATE_STOP)
    assert workflow._history[-2] == {"role": "assistant", "content": "Hello"}
    assert workflow._history[-1] == {"role": "user", "content": "second turn"}


@pytest.mark.asyncio
async def test_shallow_bridge_stops_forwarding_after_cancellation() -> None:
    class ShallowWorkflow:
        def __init__(self) -> None:
            self.produced: list[str] = []

        async def on_user_turn(self, _text: str) -> AsyncIterator[str]:
            for chunk in ("first", "not forwarded"):
                self.produced.append(chunk)
                yield chunk

    workflow = ShallowWorkflow()
    bridge = GenericWorkflowBridge(workflow)
    token = CancelToken()
    stream = bridge.invoke(AgentTurnInput.from_text("hello"), NULL_RECORDER, token)

    first = await anext(stream)
    assert first.kind == "text_delta"
    assert first.text == "first"
    token.cancel()
    remaining = [event async for event in stream]

    assert not bridge.deep_mode
    assert [event for event in remaining if event.kind == "text_delta"] == []
    assert workflow.produced == ["first", "not forwarded"]
    with pytest.raises(ShallowModeInterruptionError):
        bridge.apply_interruption("first", CancellationMode.IMMEDIATE_STOP)


@pytest.mark.asyncio
async def test_shallow_text_session_records_failed_state_notification(tmp_path: Path) -> None:
    class BlockingShallowWorkflow:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def on_user_turn(self, text: str) -> AsyncIterator[str]:
            if text == "first":
                yield "partial"
                self.started.set()
                await asyncio.Event().wait()
            else:
                yield f"reply: {text}"

    workflow = BlockingShallowWorkflow()
    bridge = GenericWorkflowBridge(workflow)
    # Full debug mode writes a durable SQLite journal; keep xdist workers out
    # of the repository-wide default data directory.
    session = create_text_session(
        agent=bridge,
        debug="full",
        wrap_agent=False,
        data_dir=tmp_path,
    )

    first_turn = asyncio.create_task(session.send_text("first"))
    await workflow.started.wait()
    assert await session.send_text("second") == "reply: second"
    cancelled = await asyncio.gather(first_turn, return_exceptions=True)
    assert isinstance(cancelled[0], asyncio.CancelledError)

    records = [
        record
        for record in session.journal.read()
        if record.name == "assistant_interruption_notified"
    ]
    assert records[-1].data == {
        "source": "text_session",
        "mode": "truncate",
        "text_spoken": "partial",
        "notified": False,
    }
    await session.stop()
