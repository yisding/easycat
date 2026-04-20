"""Local voice bot using Cartesia for both STT (Ink-Whisper) and TTS (Sonic).

Swaps both providers at once via typed configs.  The agent still runs on
OpenAI — provider choice is per-stage.

Setup:
  export OPENAI_API_KEY="..."
  export CARTESIA_API_KEY="..."
  uv sync --extra quickstart --extra cartesia
  uv run python examples/cartesia_voice.py
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
from easycat.stt.cartesia_provider import CartesiaSTTConfig
from easycat.tts.cartesia_tts import CartesiaTTSConfig


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")
    cartesia_key = require_env("CARTESIA_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")

    config = EasyCatConfig(
        openai_api_key=api_key,
        stt=CartesiaSTTConfig(api_key=cartesia_key, model="ink-whisper"),
        tts=CartesiaTTSConfig(api_key=cartesia_key, model_id="sonic-3"),
        transport=LocalTransportConfig(),
        agent=agent,
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
