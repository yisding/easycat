"""Chapter 2 — Select STT/TTS providers and voices.

Dependencies:
    uv sync --extra quickstart --extra deepgram --extra elevenlabs --group dev
    OPENAI_API_KEY
    DEEPGRAM_API_KEY (deepgram-stt and elevenlabs-voice profiles)
    ELEVENLABS_API_KEY (elevenlabs-voice profile)

Preflight:
    uv run easycat doctor
    uv run easycat doctor --json
    uv run easycat doctor --env-file .env
    uv run easycat doctor --env-file .env --json
    uv run easycat doctor --provider deepgram
    uv run easycat doctor --provider elevenlabs

Run:
    uv run python docs/using-easycat/02-providers-and-voices/main.py list
    uv run python docs/using-easycat/02-providers-and-voices/main.py openai --voice alloy
    uv run python docs/using-easycat/02-providers-and-voices/main.py deepgram-stt --voice nova
    uv run python docs/using-easycat/02-providers-and-voices/main.py elevenlabs-voice
    If keys live in .env, add `--env-file .env` after `uv run`.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Literal, cast

from easycat import (
    VoiceApp,
    available_stt_providers,
    available_tts_providers,
    require_env,
)
from easycat.tts.elevenlabs_tts import ElevenLabsTTSConfig
from easycat.tts.openai_tts import OpenAITTSConfig

Profile = Literal["list", "openai", "deepgram-stt", "elevenlabs-voice"]
PROFILES: tuple[Profile, ...] = ("list", "openai", "deepgram-stt", "elevenlabs-voice")


@dataclass(frozen=True)
class Options:
    profile: Profile
    voice: str | None


def parse_options() -> Options:
    parser = argparse.ArgumentParser(description="Compare EasyCat speech-provider specs.")
    parser.add_argument("profile", choices=PROFILES)
    parser.add_argument(
        "--voice",
        help="OpenAI voice name or ElevenLabs voice ID, depending on the profile.",
    )
    args = parser.parse_args()
    return Options(profile=cast(Profile, args.profile), voice=args.voice)


def list_providers() -> None:
    print("STT providers:", ", ".join(available_stt_providers()))
    print("TTS providers:", ", ".join(available_tts_providers()))


def build_app(options: Options) -> VoiceApp:
    try:
        from agents import Agent  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit(
            "This chapter needs the quickstart dependencies. Run "
            "`uv sync --extra quickstart --extra deepgram --extra elevenlabs --group dev`."
        ) from exc

    openai_key = require_env("OPENAI_API_KEY")
    agent = Agent(
        name="feature-guide",
        instructions="Answer in one or two friendly sentences.",
    )

    if options.profile == "openai":
        return VoiceApp(
            agent=agent,
            stt="openai-realtime",
            tts=OpenAITTSConfig(api_key=openai_key, voice=options.voice or "alloy"),
        )

    require_env("DEEPGRAM_API_KEY")
    if options.profile == "deepgram-stt":
        return VoiceApp(
            agent=agent,
            stt="deepgram/nova-2",
            tts=OpenAITTSConfig(api_key=openai_key, voice=options.voice or "alloy"),
        )

    if options.profile == "elevenlabs-voice":
        return VoiceApp(
            agent=agent,
            stt="deepgram/nova-2",
            tts=ElevenLabsTTSConfig(
                api_key=require_env("ELEVENLABS_API_KEY"),
                model_id="eleven_flash_v2_5",
                voice_id=options.voice or "EXAVITQu4vr4xnSDxMaL",
            ),
        )

    raise ValueError("The list profile does not build a VoiceApp.")


def main() -> None:
    options = parse_options()
    if options.profile == "list":
        list_providers()
        return

    build_app(options).run("local")


if __name__ == "__main__":
    main()
