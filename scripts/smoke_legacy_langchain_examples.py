"""Load the two legacy AgentExecutor examples without starting audio or network I/O."""

from __future__ import annotations

import asyncio
import os
import runpy
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableLambda

import easycat
from easycat import EasyConfig, auto_adapt_agent
from easycat.integrations.agents import LangChainBridge
from easycat.integrations.agents.base import NULL_RECORDER, AgentTurnInput


async def _smoke_bridge_runtime() -> None:
    expected = "legacy bridge runtime"
    bridge = LangChainBridge(RunnableLambda(lambda _payload: expected))
    events = [
        event async for event in bridge.invoke(AgentTurnInput.from_text("ping"), NULL_RECORDER)
    ]
    text = "".join(event.text for event in events if event.kind == "text_delta")
    done = [event for event in events if event.kind == "done"]
    if text != expected or len(done) != 1 or done[0].text != expected:
        raise RuntimeError(f"Unexpected legacy LangChain bridge events: {events!r}")


def main() -> None:
    os.environ.setdefault("OPENAI_API_KEY", "legacy-langchain-smoke")
    captured: list[EasyConfig] = []

    def capture(config: EasyConfig, *, feedback: Any = "auto") -> None:
        del feedback
        adapted = auto_adapt_agent(config.agent)
        if not isinstance(adapted, LangChainBridge):
            raise TypeError(
                f"Expected LangChainBridge for legacy AgentExecutor, got {type(adapted).__name__}"
            )
        captured.append(config)

    easycat.run = capture

    repo_root = Path(__file__).resolve().parents[1]
    examples = ("function_tools_langchain.py", "session_actions_langchain.py")
    for name in examples:
        runpy.run_path(str(repo_root / "examples" / name), run_name=f"legacy_{name[:-3]}")

    if len(captured) != len(examples):
        raise RuntimeError(f"Expected {len(examples)} example configs, captured {len(captured)}")
    asyncio.run(_smoke_bridge_runtime())
    print("legacy LangChain examples loaded successfully")


if __name__ == "__main__":
    main()
