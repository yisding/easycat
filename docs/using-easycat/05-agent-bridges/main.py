"""Chapter 5 — Choose an EasyCat agent bridge.

Dependencies:
    uv sync --extra quickstart --group dev
    OPENAI_API_KEY (run mode only, for STT and TTS)

Preflight:
    uv run easycat doctor
    uv run easycat doctor --json
    uv run easycat doctor --env-file .env
    uv run easycat doctor --env-file .env --json

Run:
    uv run python docs/using-easycat/05-agent-bridges/main.py matrix
    uv run python docs/using-easycat/05-agent-bridges/main.py run
    If the key lives in .env, add `--env-file .env` after `uv run`.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Literal, cast

from easycat import EasyConfig, VoiceApp, auto_adapt_agent, require_env
from easycat.integrations.agents import AgentRunner, GenericWorkflowBridge

Mode = Literal["matrix", "run"]


@dataclass(frozen=True)
class BridgeChoice:
    input_surface: str
    adapter: str
    install: str


BRIDGE_CHOICES = (
    BridgeChoice("agents.Agent", "OpenAIAgentsBridge", "quickstart / openai-agents"),
    BridgeChoice("pydantic_ai.Agent", "PydanticAIBridge", "pydantic-ai or pydantic-ai-v2"),
    BridgeChoice("LangChain Runnable", "LangChainBridge", "langchain + model package"),
    BridgeChoice("compiled LangGraph", "LangGraphBridge", "langgraph + model package"),
    BridgeChoice("LlamaIndex Workflow", "LlamaAgentsBridge", "llama-agents"),
    BridgeChoice("Responses API URL", "RemoteResponsesAPIBridge", "core HTTP client"),
    BridgeChoice("on_user_turn workflow", "GenericWorkflowBridge", "no framework extra"),
    BridgeChoice("async run agent", "AgentRunner", "no framework extra"),
)


class SupportWorkflow:
    """Small stateful workflow that needs no agent SDK or model API."""

    def __init__(self) -> None:
        self.turns = 0

    async def on_user_turn(self, text: str) -> str:
        self.turns += 1
        lowered = text.casefold()
        if "hour" in lowered or "open" in lowered:
            return "EasyCat support is open from nine to five Pacific time."
        if "turn" in lowered:
            return f"This is workflow turn {self.turns}."
        return "Ask me about support hours, or ask which turn this is."

    def snapshot_state(self) -> dict[str, int]:
        return {"turns": self.turns}


class PlainAgent:
    async def run(self, text: str) -> str:
        return f"Echo: {text}"


def parse_mode() -> Mode:
    parser = argparse.ArgumentParser(
        description="Print the agent-adapter matrix or run a custom workflow by voice."
    )
    parser.add_argument("mode", choices=("matrix", "run"))
    return cast(Mode, parser.parse_args().mode)


def print_matrix() -> None:
    print("Agent input -> EasyCat adapter [install]")
    for choice in BRIDGE_CHOICES:
        print(f"- {choice.input_surface} -> {choice.adapter} [{choice.install}]")

    workflow = SupportWorkflow()
    adapted_workflow = auto_adapt_agent(workflow)
    assert isinstance(adapted_workflow, GenericWorkflowBridge)
    print(
        "\nDetected SupportWorkflow -> "
        f"{type(adapted_workflow).__name__} (deep_mode={adapted_workflow.deep_mode})"
    )

    plain = PlainAgent()
    assert auto_adapt_agent(plain) is plain
    print(f"Detected PlainAgent -> unchanged; Session adds {type(AgentRunner(plain)).__name__}")


def build_live_app() -> VoiceApp:
    require_env("OPENAI_API_KEY")
    config = EasyConfig.mic(agent=SupportWorkflow())
    return VoiceApp(config=config)


def main() -> None:
    mode = parse_mode()
    if mode == "matrix":
        print_matrix()
        return
    build_live_app().run("local")


if __name__ == "__main__":
    main()
