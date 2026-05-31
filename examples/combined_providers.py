"""Local voice bot using Deepgram STT + ElevenLabs TTS — stages compose.

Each per-stage example (``deepgram_voice.py``, ``elevenlabs_voice.py``,
``cartesia_voice.py``) uses one provider for both stages. This one
mixes vendors to show that STT and TTS swap independently.

Setup: export OPENAI_API_KEY=...; export DEEPGRAM_API_KEY=...; export ELEVENLABS_API_KEY=...
       uv sync --extra quickstart --extra deepgram --extra elevenlabs
Run:   uv run python examples/combined_providers.py
"""

try:
    from agents import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. Install with: uv sync --extra quickstart"
    ) from exc

from easycat import EasyConfig, require_env, run

require_env("OPENAI_API_KEY")
require_env("DEEPGRAM_API_KEY")  # consumed by the stt= shortcut below
require_env("ELEVENLABS_API_KEY")  # consumed by the tts= shortcut below

# Two string shortcuts, two different vendors — STT and TTS swap
# independently. auto-align matches the TTS output to the transport's
# rate (the default mic transport is 24 kHz).
run(
    EasyConfig.mic(
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
        stt="deepgram/nova-2",
        tts="elevenlabs/eleven_flash_v2_5",
    )
)
