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

from easycat import PCM16_MONO_16K, EasyConfig, LocalTransportConfig, require_env, run
from easycat.stt.cartesia_provider import CartesiaSTTConfig
from easycat.tts.cartesia_tts import CartesiaTTSConfig

require_env("OPENAI_API_KEY")
cartesia_key = require_env("CARTESIA_API_KEY")

# Pin both stages and the transport to 16 kHz; LocalTransport plays PCM verbatim.
run(
    EasyConfig.mic(
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
        stt=CartesiaSTTConfig(api_key=cartesia_key, model="ink-whisper"),
        tts=CartesiaTTSConfig(
            api_key=cartesia_key,
            model_id="sonic-3",
            sample_rate=16000,
            output_format=PCM16_MONO_16K,
        ),
        transport=LocalTransportConfig(audio_format=PCM16_MONO_16K),
    )
)
