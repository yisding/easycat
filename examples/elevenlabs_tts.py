"""Local voice bot using OpenAI STT + ElevenLabs TTS.

Uses the typed-config path (``ElevenLabsTTSConfig``) so voice_id and
model_id can be customized.  The string shortcut ``tts="elevenlabs"`` works
too, but only when defaults are acceptable.

Setup:
  export OPENAI_API_KEY="..."
  export ELEVENLABS_API_KEY="..."
  uv sync --extra quickstart --extra elevenlabs
  uv run python examples/elevenlabs_tts.py
"""

from __future__ import annotations

import asyncio

from easycat import (
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
    elevenlabs_key = require_env("ELEVENLABS_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")

    config = EasyCatConfig(
        openai_api_key=api_key,
        tts=ElevenLabsTTSConfig(
            api_key=elevenlabs_key,
            voice_id="EXAVITQu4vr4xnSDxMaL",  # Sarah — override for production
            model_id="eleven_flash_v2_5",
        ),
        transport=LocalTransportConfig(),
        agent=agent,
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
