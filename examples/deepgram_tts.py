"""Local voice bot using OpenAI STT + Deepgram TTS (Aura).

Mirror of ``elevenlabs_tts.py`` for the Deepgram Aura voices.  Uses the
typed-config path (``DeepgramTTSConfig``) so the Aura voice model can be
customized.  The string shortcut ``tts="deepgram/aura-asteria-en"`` also
works when defaults are fine.

Setup:
  export OPENAI_API_KEY="..."
  export DEEPGRAM_API_KEY="..."
  uv sync --extra quickstart --extra deepgram
  uv run python examples/deepgram_tts.py
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
from easycat.tts.deepgram_tts import DeepgramTTSConfig


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")
    deepgram_key = require_env("DEEPGRAM_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")

    config = EasyCatConfig(
        openai_api_key=api_key,
        tts=DeepgramTTSConfig(
            api_key=deepgram_key,
            model="aura-asteria-en",  # swap for any Aura voice
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
