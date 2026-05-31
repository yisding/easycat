"""Local voice bot using Deepgram for both STT (Nova-2) and TTS (Aura).

Setup: export OPENAI_API_KEY=...; export DEEPGRAM_API_KEY=...
       uv sync --extra quickstart --extra deepgram
Run:   uv run python examples/deepgram_voice.py
"""

try:
    from agents import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. Install with: uv sync --extra quickstart"
    ) from exc

from easycat import EasyConfig, require_env, run

require_env("OPENAI_API_KEY")
require_env("DEEPGRAM_API_KEY")  # consumed by the string shortcuts below

# One token per stage swaps the provider. The shortcut reads
# DEEPGRAM_API_KEY from the environment, and auto-align matches the TTS
# output to the transport's rate (the default mic transport is 24 kHz).
run(
    EasyConfig.mic(
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
        stt="deepgram/nova-2",
        tts="deepgram/aura-asteria-en",
    )
)

# Need a fixed rate (e.g. 16 kHz telephony realism)? Reach for the typed
# config instead of the string shortcut and pin the transport to match:
#   from easycat import PCM16_MONO_16K, LocalTransportConfig
#   from easycat.tts.deepgram_tts import DeepgramTTSConfig
#   stt="deepgram/nova-2",
#   tts=DeepgramTTSConfig(sample_rate=16000, output_format=PCM16_MONO_16K),
#   transport=LocalTransportConfig(audio_format=PCM16_MONO_16K),
