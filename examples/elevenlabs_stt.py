"""Local voice bot using ElevenLabs STT + OpenAI TTS.

Mirror of ``deepgram_stt.py`` for the ElevenLabs Scribe models.  The typed
config lets you pick realtime (``scribe_v2_realtime``) or batch
(``scribe_v1``) mode and an explicit language, which the string shortcut
``stt="elevenlabs"`` cannot express.

Setup:
  export OPENAI_API_KEY="..."
  export ELEVENLABS_API_KEY="..."
  uv sync --extra quickstart --extra elevenlabs
  uv run python examples/elevenlabs_stt.py
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
from easycat.stt.elevenlabs_provider import ElevenLabsSTTConfig


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")
    elevenlabs_key = require_env("ELEVENLABS_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")

    # ElevenLabs realtime STT expects 16 kHz PCM; align the transport so
    # frames reach STT without needing an in-pipeline resample.
    config = EasyCatConfig(
        openai_api_key=api_key,
        stt=ElevenLabsSTTConfig(
            api_key=elevenlabs_key,
            mode="realtime",
            realtime_sample_rate=16000,
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
