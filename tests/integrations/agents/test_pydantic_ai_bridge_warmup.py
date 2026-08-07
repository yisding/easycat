from __future__ import annotations

import asyncio
from typing import Any, Self

import pytest

import easycat.integrations.agents.pydantic_ai as pydantic_ai_module
from easycat.integrations.agents.pydantic_ai import PydanticAIBridge


class _FakeModels:
    def __init__(self) -> None:
        self.retrieved: list[str] = []

    async def retrieve(self, model_name: str) -> None:
        self.retrieved.append(model_name)


class _FakeModel:
    model_name = "model-1"

    def __init__(self) -> None:
        self.client = type("Client", (), {"models": _FakeModels()})()


class _FakeAgent:
    def __init__(self, model: Any) -> None:
        self.model = model
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> Self:
        self.entered += 1
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.exited += 1


class _BlockingEnterAgent(_FakeAgent):
    def __init__(self) -> None:
        super().__init__(_FakeModel())
        self.enter_started = asyncio.Event()
        self.release_enter = asyncio.Event()

    async def __aenter__(self) -> Self:
        self.entered += 1
        self.enter_started.set()
        await self.release_enter.wait()
        return self


@pytest.mark.asyncio
async def test_warmup_resolves_and_reuses_the_agent_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = pytest.importorskip("pydantic_ai.models")

    resolved_model = _FakeModel()
    monkeypatch.setattr(models, "infer_model", lambda _model: resolved_model)
    agent = _FakeAgent("openai:model-1")
    bridge = PydanticAIBridge(agent=agent)

    await bridge.warmup()
    await bridge.warmup()

    assert agent.model is resolved_model
    assert agent.entered == 1
    assert resolved_model.client.models.retrieved == ["model-1"]

    await bridge.aclose()
    assert agent.exited == 1


@pytest.mark.asyncio
async def test_graph_warmup_enters_and_closes_each_static_agent() -> None:
    first = _FakeAgent(_FakeModel())
    second = _FakeAgent(_FakeModel())
    state = type("State", (), {"_easycat_event_handler": None})
    bridge = PydanticAIBridge(
        graph=object(),
        state_factory=state,
        initial_node_factory=lambda _prompt, _state: object(),
        agents=[first, second],
    )

    await bridge.warmup()
    await bridge.aclose()

    assert first.entered == first.exited == 1
    assert second.entered == second.exited == 1
    assert first.model.client.models.retrieved == ["model-1"]
    assert second.model.client.models.retrieved == ["model-1"]


@pytest.mark.asyncio
async def test_warmup_bounds_and_rolls_back_blocked_context_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pydantic_ai_module,
        "_PYDANTIC_AI_WARMUP_TIMEOUT_SECONDS",
        0.01,
    )
    agent = _BlockingEnterAgent()
    bridge = PydanticAIBridge(agent=agent)

    await bridge.warmup()

    assert agent.entered == 1
    assert agent.exited == 1


@pytest.mark.asyncio
async def test_concurrent_warmups_enter_agent_once() -> None:
    agent = _BlockingEnterAgent()
    bridge = PydanticAIBridge(agent=agent)

    first = asyncio.create_task(bridge.warmup())
    await agent.enter_started.wait()
    second = asyncio.create_task(bridge.warmup())
    await asyncio.sleep(0)

    assert agent.entered == 1

    agent.release_enter.set()
    await asyncio.gather(first, second)

    assert agent.entered == 1
    await bridge.aclose()
    assert agent.exited == 1


@pytest.mark.asyncio
async def test_close_overlapping_warmup_leaves_no_entered_context() -> None:
    agent = _BlockingEnterAgent()
    bridge = PydanticAIBridge(agent=agent)

    warming = asyncio.create_task(bridge.warmup())
    await agent.enter_started.wait()
    closing = asyncio.create_task(bridge.aclose())
    await asyncio.sleep(0)

    assert not closing.done()

    agent.release_enter.set()
    await asyncio.gather(warming, closing)

    assert agent.entered == agent.exited == 1
    await bridge.warmup()
    assert agent.entered == 1


@pytest.mark.asyncio
async def test_startup_rollback_closes_context_but_allows_warmup_retry() -> None:
    agent = _FakeAgent(_FakeModel())
    bridge = PydanticAIBridge(agent=agent)

    await bridge.warmup()
    await bridge.rollback_warmup()
    await bridge.warmup()

    assert agent.entered == 2
    assert agent.exited == 1

    await bridge.aclose()
    assert agent.exited == 2
