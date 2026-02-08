"""Tests for BaseAgentAdapter shared functionality."""

from __future__ import annotations

import pytest

from easycat.agents.base import BaseAgentAdapter


class ConcreteAdapter(BaseAgentAdapter):
    """Minimal subclass for testing base behaviour."""

    async def run(self, text: str) -> str:
        return f"echo: {text}"


# ── History management ────────────────────────────────────────────


def test_initial_history_is_empty():
    adapter = ConcreteAdapter()
    assert adapter.message_history == []


def test_clear_history():
    adapter = ConcreteAdapter()
    adapter._message_history = [{"role": "user", "content": "hi"}]
    assert len(adapter.message_history) == 1

    adapter.clear_history()
    assert adapter.message_history == []


def test_message_history_returns_copy():
    adapter = ConcreteAdapter()
    adapter._message_history = [{"role": "user", "content": "hi"}]
    copy = adapter.message_history
    copy.append({"role": "assistant", "content": "bye"})
    assert len(adapter.message_history) == 1  # original unchanged


# ── Subclass contract ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_not_implemented():
    adapter = BaseAgentAdapter()
    with pytest.raises(NotImplementedError):
        await adapter.run("test")


@pytest.mark.asyncio
async def test_run_streaming_not_implemented():
    adapter = BaseAgentAdapter()
    with pytest.raises(NotImplementedError):
        async for _ in adapter.run_streaming("test"):
            pass


# ── Both adapters inherit from base ──────────────────────────────


def test_pydantic_adapter_inherits_from_base():
    from easycat.agents.pydantic_ai import PydanticAIAdapter

    class FakeAgent:
        pass

    adapter = PydanticAIAdapter(FakeAgent())
    assert isinstance(adapter, BaseAgentAdapter)


def test_openai_adapter_inherits_from_base():
    from easycat.agents.openai_agents import OpenAIAgentsAdapter

    adapter = OpenAIAgentsAdapter(object())
    assert isinstance(adapter, BaseAgentAdapter)
