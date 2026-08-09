"""Smoke the documented examples and bridge runtime for one LangChain line."""

from __future__ import annotations

import argparse
import asyncio
import os
import runpy
from importlib.metadata import version
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableLambda

import easycat
from easycat import EasyConfig, auto_adapt_agent
from easycat.integrations.agents import LangChainBridge
from easycat.integrations.agents.base import NULL_RECORDER, AgentTurnInput

EXAMPLES_BY_LINE = {
    "v0": ("function_tools_langchain.py", "session_actions_langchain.py"),
    "v1": ("langchain_voice.py",),
}


async def _smoke_bridge_runtime(line: str) -> None:
    expected = f"LangChain {line} bridge runtime"
    bridge = LangChainBridge(RunnableLambda(lambda _payload: expected))
    events = [
        event async for event in bridge.invoke(AgentTurnInput.from_text("ping"), NULL_RECORDER)
    ]
    text = "".join(event.text for event in events if event.kind == "text_delta")
    done = [event for event in events if event.kind == "done"]
    if text != expected or len(done) != 1 or done[0].text != expected:
        raise RuntimeError(f"Unexpected LangChain {line} bridge events: {events!r}")


def _assert_installed_line(line: str) -> None:
    installed = version("langchain")
    major = installed.partition(".")[0]
    expected_major = {"v0": "0", "v1": "1"}[line]
    if major != expected_major:
        raise RuntimeError(f"Expected LangChain {line}, found langchain=={installed}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--line", choices=sorted(EXAMPLES_BY_LINE), required=True)
    args = parser.parse_args(argv)

    _assert_installed_line(args.line)
    os.environ.setdefault("OPENAI_API_KEY", f"langchain-{args.line}-smoke")
    captured: list[EasyConfig] = []

    def capture(config: EasyConfig, *, feedback: Any = "auto") -> None:
        del feedback
        adapted = auto_adapt_agent(config.agent)
        if not isinstance(adapted, LangChainBridge):
            raise TypeError(
                f"Expected LangChainBridge for LangChain {args.line}, got {type(adapted).__name__}"
            )
        captured.append(config)

    easycat.run = capture

    repo_root = Path(__file__).resolve().parents[1]
    examples = EXAMPLES_BY_LINE[args.line]
    for name in examples:
        runpy.run_path(str(repo_root / "examples" / name), run_name=f"{args.line}_{name[:-3]}")

    if len(captured) != len(examples):
        raise RuntimeError(f"Expected {len(examples)} example configs, captured {len(captured)}")
    asyncio.run(_smoke_bridge_runtime(args.line))
    print(f"LangChain {args.line} examples and bridge loaded successfully")


if __name__ == "__main__":
    main()
