"""Local voice bot using Cartesia for both STT (Ink-Whisper) and TTS (Sonic).

Setup: export OPENAI_API_KEY=...; export CARTESIA_API_KEY=...
       uv sync --extra quickstart --extra cartesia
Run:   uv run python examples/cartesia_voice.py
"""

try:
    from agents import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. Install with: uv sync --extra quickstart"
    ) from exc

from easycat import EasyConfig, require_env, run

require_env("OPENAI_API_KEY")
require_env("CARTESIA_API_KEY")  # consumed by the string shortcuts below

# One token per stage swaps the provider. The shortcut reads
# CARTESIA_API_KEY from the environment, and auto-align matches the TTS
# output to the transport's rate (the default mic transport is 24 kHz).
run(
    EasyConfig.mic(
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
        stt="cartesia/ink-whisper",
        tts="cartesia/sonic-3",
    )
)
