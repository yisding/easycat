"""Chapter 0 — Your First VoiceApp.

Dependencies:
    uv sync --extra quickstart --group dev
    OPENAI_API_KEY

Preflight:
    uv run easycat doctor
    uv run easycat doctor --json
    uv run easycat doctor --env-file .env
    uv run easycat doctor --env-file .env --json

Run:
    uv run python docs/using-easycat/00-first-voice-app/main.py
    If the key lives in .env, add `--env-file .env` after `uv run`.
"""

from __future__ import annotations

try:
    from agents import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "This chapter needs the quickstart dependencies. Run "
        "`uv sync --extra quickstart --group dev`."
    ) from exc

from easycat import VoiceApp, require_env


def build_app() -> VoiceApp:
    """Build the product once; the runtime mode is chosen when it runs."""
    require_env("OPENAI_API_KEY")
    agent = Agent(
        name="feature-guide",
        instructions="Answer in one or two friendly sentences.",
    )
    return VoiceApp(agent=agent)


def main() -> None:
    app = build_app()
    app.run("local")


if __name__ == "__main__":
    main()
