"""Chapter 3 companion — drive turns from stdin instead of VAD.

Dependencies:
    uv sync --extra quickstart --group dev
    OPENAI_API_KEY

Preflight:
    uv run easycat doctor
    uv run easycat doctor --json
    uv run easycat doctor --env-file .env
    uv run easycat doctor --env-file .env --json

Run:
    uv run python docs/using-easycat/03-conversation-controls/push_to_talk.py
    If the key lives in .env, add `--env-file .env` after `uv run`.
"""

from __future__ import annotations

from easycat import (
    AudioProcessingConfig,
    EasyConfig,
    TurnManagerConfig,
    TurnMode,
    create_session,
    require_env,
)
from easycat.push_to_talk import run_stdin_push_to_talk_session


def main() -> None:
    try:
        from agents import Agent  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit(
            "This chapter needs the quickstart dependencies. Run "
            "`uv sync --extra quickstart --group dev`."
        ) from exc

    require_env("OPENAI_API_KEY")
    config = EasyConfig.mic(
        agent=Agent(
            name="feature-guide",
            instructions="Answer in one or two friendly sentences.",
        ),
        audio_processing=AudioProcessingConfig(smart_turn=False),
        turn_taking=TurnManagerConfig(mode=TurnMode.PUSH_TO_TALK),
    )
    run_stdin_push_to_talk_session(create_session(config))


if __name__ == "__main__":
    main()
