"""Inject a custom VAD provider via EasyConfig.

The mirror of ``custom_stt_provider.py`` and ``custom_tts_provider.py``
for the VAD stage.  Real custom-VAD use cases include: a domain-tuned
speech detector, a tee that mirrors VAD events to a debug sink, or a
deterministic stub that fires speech start/stop on schedule for tests.

Here we wrap whichever VAD ``create_vad`` selects (Silero → FunASR →
TEN → Krisp) with ``LoggingVAD``, then pass it as
``EasyConfig.mic(vad=...)``.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart --group dev
  uv run easycat doctor
  uv run easycat doctor --env-file .env  # if keys live in .env
  uv run python examples/custom_vad_provider.py
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from easycat import (
    EasyConfig,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)
from easycat.audio_format import AudioChunk
from easycat.events import Event
from easycat.providers import VADProvider
from easycat.vad import VADConfig, create_vad


class LoggingVAD:
    """Wraps any ``VADProvider`` and prints each event on its way out.

    Implements the ``VADProvider`` Protocol structurally — no base class.
    """

    def __init__(self, inner: VADProvider) -> None:
        self._inner = inner

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        async for event in self._inner.process(chunk):
            print(f"[vad] {type(event).__name__}")
            yield event

    def configure(
        self,
        *,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 150,
        sensitivity: float = 0.5,
    ) -> None:
        self._inner.configure(
            min_speech_duration_ms=min_speech_duration_ms,
            min_silence_duration_ms=min_silence_duration_ms,
            sensitivity=sensitivity,
        )

    def version_info(self) -> dict[str, str]:
        return {**self._inner.version_info(), "wrapper": "logging"}


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    vad = LoggingVAD(create_vad(VADConfig()))

    config = EasyConfig.mic(
        openai_api_key=api_key,
        vad=vad,
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
