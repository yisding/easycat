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

from easycat import PCM16_MONO_16K, EasyConfig, LocalTransportConfig, require_env, run
from easycat.tts.deepgram_tts import DeepgramTTSConfig

require_env("OPENAI_API_KEY")
deepgram_key = require_env("DEEPGRAM_API_KEY")

# Pin both stages and the transport to 16 kHz; LocalTransport plays PCM verbatim.
run(
    EasyConfig.mic(
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
        stt="deepgram/nova-2",
        tts=DeepgramTTSConfig(
            api_key=deepgram_key,
            model="aura-asteria-en",
            sample_rate=16000,
            output_format=PCM16_MONO_16K,
        ),
        transport=LocalTransportConfig(audio_format=PCM16_MONO_16K),
    )
)
