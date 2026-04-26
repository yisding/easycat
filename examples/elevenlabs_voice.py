"""Local voice bot using ElevenLabs for both STT (Scribe) and TTS (Flash).

Setup: export OPENAI_API_KEY=...; export ELEVENLABS_API_KEY=...
       uv sync --extra quickstart --extra elevenlabs
Run:   uv run python examples/elevenlabs_voice.py
"""

try:
    from agents import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. Install with: uv sync --extra quickstart"
    ) from exc

from easycat import PCM16_MONO_16K, EasyConfig, LocalTransportConfig, require_env, run
from easycat.stt.elevenlabs_provider import ElevenLabsSTTConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTSConfig

require_env("OPENAI_API_KEY")
elevenlabs_key = require_env("ELEVENLABS_API_KEY")

# Pin both stages and the transport to 16 kHz; LocalTransport plays PCM verbatim.
run(
    EasyConfig.mic(
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
        stt=ElevenLabsSTTConfig(
            api_key=elevenlabs_key, mode="realtime", realtime_sample_rate=16000
        ),
        tts=ElevenLabsTTSConfig(
            api_key=elevenlabs_key,
            voice_id="EXAVITQu4vr4xnSDxMaL",  # Sarah
            model_id="eleven_flash_v2_5",
            output_format="pcm_16000",
            audio_format=PCM16_MONO_16K,
        ),
        transport=LocalTransportConfig(audio_format=PCM16_MONO_16K),
    )
)
