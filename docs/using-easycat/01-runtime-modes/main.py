"""Chapter 1 — Run one VoiceApp in four runtime modes.

Dependencies:
    uv sync --extra quickstart --extra webrtc --extra telephony --group dev
    OPENAI_API_KEY
    TWILIO_STREAM_URL and TWILIO_AUTH_TOKEN (twilio mode only)

Preflight:
    uv run easycat doctor
    uv run easycat doctor --json
    uv run easycat doctor --env-file .env
    uv run easycat doctor --env-file .env --json

Run:
    uv run python docs/using-easycat/01-runtime-modes/main.py local
    uv run python docs/using-easycat/01-runtime-modes/main.py browser
    uv run python docs/using-easycat/01-runtime-modes/main.py websocket
    uv run python docs/using-easycat/01-runtime-modes/main.py twilio
    If keys live in .env, add `--env-file .env` after `uv run`.
"""

from __future__ import annotations

import argparse
from typing import Literal, cast

from easycat import VoiceApp, require_env

Mode = Literal["local", "browser", "websocket", "twilio"]
MODES: tuple[Mode, ...] = ("local", "browser", "websocket", "twilio")


def parse_mode() -> Mode:
    parser = argparse.ArgumentParser(description="Run one VoiceApp in another mode.")
    parser.add_argument("mode", choices=MODES)
    return cast(Mode, parser.parse_args().mode)


def build_app() -> VoiceApp:
    try:
        from agents import Agent  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit(
            "This chapter needs the quickstart dependencies. Run "
            "`uv sync --extra quickstart --extra webrtc --extra telephony --group dev`."
        ) from exc

    require_env("OPENAI_API_KEY")
    agent = Agent(
        name="feature-guide",
        instructions="Answer in one or two friendly sentences.",
    )
    return VoiceApp(agent=agent)


def main() -> None:
    mode = parse_mode()
    app = build_app()

    if mode == "twilio":
        app.run(
            mode,
            stream_url=require_env("TWILIO_STREAM_URL"),
            twilio_auth_token=require_env("TWILIO_AUTH_TOKEN"),
        )
        return

    app.run(mode)


if __name__ == "__main__":
    main()
