"""Local voice bot demo using a single PydanticAI agent.

For function tools see ``examples/function_tools_pydantic.py``;
for multi-agent workflows see ``examples/pydantic_ai_workflow_voice.py``.

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart --extra pydantic-ai --group dev
       uv run easycat doctor
       uv run easycat doctor --env-file .env  # if keys live in .env
Run:   uv run python examples/pydantic_ai_voice.py
       uv run --env-file .env python examples/pydantic_ai_voice.py  # if keys live in .env
"""

try:
    from pydantic_ai import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "PydanticAI is required. For an app, run: "
        "uv add 'easycat[quickstart,pydantic-ai]'. In this repo, run: "
        "uv sync --extra quickstart --extra pydantic-ai --group dev"
    ) from exc

from easycat import EasyConfig, require_env, run

require_env("OPENAI_API_KEY")

run(
    EasyConfig.mic(
        agent=Agent("openai:gpt-5.2", system_prompt="You are a helpful voice assistant.")
    )
)
