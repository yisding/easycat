"""Chapter 3 — Compare EasyCat conversation-control profiles.

Dependencies:
    uv sync --extra quickstart --group dev
    OPENAI_API_KEY

Preflight:
    uv run easycat doctor
    uv run easycat doctor --json
    uv run easycat doctor --env-file .env
    uv run easycat doctor --env-file .env --json

Run:
    uv run python docs/using-easycat/03-conversation-controls/main.py balanced
    uv run python docs/using-easycat/03-conversation-controls/main.py vad-only
    uv run python docs/using-easycat/03-conversation-controls/main.py fast
    uv run python docs/using-easycat/03-conversation-controls/main.py clean
    uv run python docs/using-easycat/03-conversation-controls/main.py raw
    If the key lives in .env, add `--env-file .env` after `uv run`.
"""

from __future__ import annotations

import argparse
from typing import Literal, cast

from easycat import (
    EasyConfig,
    TurnManagerConfig,
    VoiceApp,
    require_env,
)

Profile = Literal["balanced", "vad-only", "fast", "clean", "raw"]
PROFILES: tuple[Profile, ...] = ("balanced", "vad-only", "fast", "clean", "raw")


def parse_profile() -> Profile:
    parser = argparse.ArgumentParser(description="Compare conversation-control profiles.")
    parser.add_argument("profile", choices=PROFILES)
    return cast(Profile, parser.parse_args().profile)


def profile_config(profile: Profile) -> tuple[dict[str, object], TurnManagerConfig]:
    if profile == "balanced":
        return {}, TurnManagerConfig()
    if profile == "vad-only":
        return (
            {"smart_turn": False, "enable_echo_cancellation": True},
            TurnManagerConfig(
                end_of_turn_silence_ms=700,
                punctuated_end_of_turn_silence_ms=250,
                pre_roll_ms=450,
            ),
        )
    if profile == "fast":
        return (
            {
                "smart_turn": True,
                "smart_turn_sensitivity": 0.7,
                "enable_echo_cancellation": True,
            },
            TurnManagerConfig(
                end_of_turn_silence_ms=400,
                punctuated_end_of_turn_silence_ms=180,
                pre_roll_ms=450,
            ),
        )
    if profile == "clean":
        return (
            {
                "smart_turn": True,
                "smart_turn_sensitivity": 0.6,
                "enable_noise_reduction": True,
                "enable_echo_cancellation": True,
            },
            TurnManagerConfig(),
        )
    return (
        {
            "smart_turn": False,
            "enable_noise_reduction": False,
            "enable_echo_cancellation": False,
        },
        TurnManagerConfig(end_of_turn_silence_ms=700),
    )


def build_config(profile: Profile) -> EasyConfig:
    try:
        from agents import Agent  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit(
            "This chapter needs the quickstart dependencies. Run "
            "`uv sync --extra quickstart --group dev`."
        ) from exc

    require_env("OPENAI_API_KEY")
    audio_options, turn_taking = profile_config(profile)
    return EasyConfig.mic(
        agent=Agent(
            name="feature-guide",
            instructions="Answer in one or two friendly sentences.",
        ),
        turn_taking=turn_taking,
        **audio_options,
    )


def main() -> None:
    config = build_config(parse_profile())
    VoiceApp(config=config).run("local")


if __name__ == "__main__":
    main()
