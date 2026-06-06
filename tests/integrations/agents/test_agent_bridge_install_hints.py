"""Install hint coverage for optional agent bridge adapters."""

from __future__ import annotations

import builtins
from typing import Any

import pytest

from easycat.integrations.agents import llama_agents as llama_agents_module
from easycat.integrations.agents import openai_agents as openai_agents_module
from easycat.integrations.agents.base import NULL_RECORDER, AgentTurnInput


def _assert_bridge_install_hint(message: str, extra: str) -> None:
    assert f"uv add 'easycat[{extra}]'" in message
    assert f"From the EasyCat repo, use: uv sync --extra {extra} --group dev" in message


@pytest.mark.asyncio
async def test_openai_agents_bridge_missing_sdk_install_hint(monkeypatch):
    monkeypatch.setattr(openai_agents_module, "Runner", None)
    bridge = openai_agents_module.OpenAIAgentsBridge(agent=object())

    with pytest.raises(ImportError) as exc_info:
        async for _ in bridge.invoke(AgentTurnInput.from_text("hello"), NULL_RECORDER):
            pass

    message = str(exc_info.value)
    _assert_bridge_install_hint(message, "openai-agents")


def test_llama_agents_bridge_missing_remote_client_install_hint(monkeypatch):
    real_import = builtins.__import__

    def fail_llama_agents_import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name in {"llama_agents", "llama_agents.client"}:
            raise ImportError("missing llama-agents-client")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fail_llama_agents_import)

    with pytest.raises(ImportError) as exc_info:
        llama_agents_module.LlamaAgentsBridge(
            base_url="http://example.test",
            workflow_name="support",
        )

    message = str(exc_info.value)
    _assert_bridge_install_hint(message, "llama-agents")
