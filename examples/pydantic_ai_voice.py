"""Local voice bot demo using a single PydanticAI agent.

For function tools see ``examples/function_tools_pydantic.py``;
for multi-agent workflows see ``examples/pydantic_ai_workflow_voice.py``.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart; uv add easycat[pydantic-ai]
Run:   uv run python examples/pydantic_ai_voice.py
"""

from pydantic_ai import Agent  # type: ignore[import-untyped]

from easycat import EasyCatConfig, run

run(
    EasyCatConfig.mic(
        agent=Agent("openai:gpt-5.2", system_prompt="You are a helpful voice assistant.")
    )
)
