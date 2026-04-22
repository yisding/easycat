"""Local voice bot using Deepgram STT + OpenAI TTS.

Shows the simplest provider swap: the string shortcut ``stt="deepgram/nova-2"``
reads ``DEEPGRAM_API_KEY`` from the environment and wires a
``DeepgramSTTConfig`` automatically.  TTS stays on OpenAI via the default
chain triggered by ``OPENAI_API_KEY``.

Setup:
  export OPENAI_API_KEY="..."
  export DEEPGRAM_API_KEY="..."
  uv sync --extra quickstart --extra deepgram
  uv run python examples/deepgram_stt.py
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


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")
    require_env("DEEPGRAM_API_KEY")  # consumed by the string shortcut below

    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")

    # Deepgram's default sample rate is 16 kHz; align the transport so mic
    # frames reach STT without rate mismatch. OpenAI TTS adopts the
    # transport format automatically.
    config = EasyCatConfig(
        openai_api_key=api_key,
        stt="deepgram/nova-2",
        transport=LocalTransportConfig(audio_format=PCM16_MONO_16K),
        agent=agent,
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
