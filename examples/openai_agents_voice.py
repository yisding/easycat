"""Local voice bot demo using a single OpenAI Agents SDK agent.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart
Run:   uv run python examples/openai_agents_voice.py
"""

try:
    from agents import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. Install with: uv sync --extra quickstart"
    ) from exc

from easycat import EasyCatConfig, run

run(
    EasyCatConfig.mic(
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant.")
    )
)
