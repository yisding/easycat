"""Local voice bot using Deepgram STT + ElevenLabs TTS together.

Each per-stage example (``deepgram_stt.py``, ``elevenlabs_tts.py``,
``cartesia_voice.py``) swaps a single provider.  This one shows that the
swaps compose: pick STT and TTS independently and the rest of the
pipeline (VAD, agent, transport) does not change.

The agent still runs on OpenAI; provider choice is per-stage.

Setup:
  export OPENAI_API_KEY="..."
  export DEEPGRAM_API_KEY="..."
  export ELEVENLABS_API_KEY="..."
  uv sync --extra quickstart --extra deepgram --extra elevenlabs
  uv run python examples/combined_providers.py
"""

from __future__ import annotations

import asyncio

from easycat import (
    PCM16_MONO_16K,
    EasyCatConfig,
    LocalTransportConfig,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)
from easycat.tts.elevenlabs_tts import ElevenLabsTTSConfig


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")
    require_env("DEEPGRAM_API_KEY")  # consumed by the string shortcut below
    elevenlabs_key = require_env("ELEVENLABS_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")

    # Deepgram STT defaults to 16 kHz and ElevenLabs TTS defaults to 24 kHz.
    # Pin both stages and the transport to 16 kHz so mic frames reach STT
    # and TTS output reaches the speaker at matching rates — the local
    # transport plays PCM verbatim without resampling, so a TTS/transport
    # mismatch would garble playback pitch.
    config = EasyCatConfig(
        openai_api_key=api_key,
        stt="deepgram/nova-2",
        tts=ElevenLabsTTSConfig(
            api_key=elevenlabs_key,
            voice_id="EXAVITQu4vr4xnSDxMaL",  # Sarah — override for production
            model_id="eleven_flash_v2_5",
            output_format="pcm_16000",
            audio_format=PCM16_MONO_16K,
        ),
        transport=LocalTransportConfig(audio_format=PCM16_MONO_16K),
        agent=agent,
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
