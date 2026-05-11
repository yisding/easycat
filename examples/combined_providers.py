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

from easycat import PCM16_MONO_16K, EasyConfig, LocalTransportConfig, require_env, run
from easycat.tts.elevenlabs_tts import ElevenLabsTTSConfig

require_env("OPENAI_API_KEY")
require_env("DEEPGRAM_API_KEY")  # consumed by the string shortcut below
elevenlabs_key = require_env("ELEVENLABS_API_KEY")

# Pin all stages and the transport to 16 kHz; LocalTransport plays PCM verbatim.
run(
    EasyConfig.mic(
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
        stt="deepgram/nova-2",
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
