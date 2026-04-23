"""Local voice bot using Deepgram for both STT (Nova-2) and TTS (Aura).

Swaps both speech stages to Deepgram via typed configs and the
``stt="deepgram/nova-2"`` string shortcut.  Only ``DEEPGRAM_API_KEY`` is
needed for speech; the OpenAI key is still required because the agent
runs on the OpenAI Agents SDK.

Setup:
  export OPENAI_API_KEY="..."
  export DEEPGRAM_API_KEY="..."
  uv sync --extra quickstart --extra deepgram
  uv run python examples/deepgram_voice.py
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
from easycat.tts.deepgram_tts import DeepgramTTSConfig


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")
    deepgram_key = require_env("DEEPGRAM_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")

    # Deepgram STT defaults to 16 kHz and Deepgram TTS defaults to 24 kHz.
    # Pin both stages and the transport to 16 kHz so mic frames reach STT
    # and TTS output reaches the speaker at matching rates — the local
    # transport plays PCM verbatim without resampling.
    config = EasyCatConfig(
        openai_api_key=api_key,
        stt="deepgram/nova-2",  # reads DEEPGRAM_API_KEY from env
        tts=DeepgramTTSConfig(
            api_key=deepgram_key,
            model="aura-asteria-en",
            sample_rate=16000,
            output_format=PCM16_MONO_16K,
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
