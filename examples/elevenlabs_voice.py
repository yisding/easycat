"""Local voice bot using ElevenLabs for both STT (Scribe) and TTS (Flash).

Setup: export OPENAI_API_KEY=...; export ELEVENLABS_API_KEY=...
       uv sync --extra quickstart --extra elevenlabs --group dev
       uv run easycat doctor
       uv run easycat doctor --env-file .env  # if keys live in .env
       uv run easycat doctor --env-file .env --json  # for parseable checks
Run:   uv run python examples/elevenlabs_voice.py
       uv run --env-file .env python examples/elevenlabs_voice.py  # if keys live in .env
"""

try:
    from agents import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. For an app, run: "
        "uv add 'easycat[quickstart,elevenlabs]'. In this repo, run: "
        "uv sync --extra quickstart --extra elevenlabs --group dev"
    ) from exc

from easycat import EasyConfig, require_env, run

require_env("OPENAI_API_KEY")
require_env("ELEVENLABS_API_KEY")  # consumed by the string shortcuts below

# One token per stage swaps the provider. The shortcut reads
# ELEVENLABS_API_KEY from the environment, and auto-align matches the TTS
# output to the transport's rate (the default mic transport is 24 kHz).
run(
    EasyConfig.mic(
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
        stt="elevenlabs",
        tts="elevenlabs/eleven_flash_v2_5",
    )
)
