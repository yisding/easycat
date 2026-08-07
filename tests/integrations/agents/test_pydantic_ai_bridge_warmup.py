from __future__ import annotations

from typing import Any, Self

import pytest

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


@pytest.mark.asyncio
async def test_warmup_resolves_and_reuses_the_agent_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic_ai import models

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
