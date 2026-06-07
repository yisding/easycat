"""Inject a custom TTS provider via EasyConfig.

The mirror of ``custom_stt_provider.py``: supply a structurally-typed
``TTSProvider`` as ``EasyConfig.mic(tts=...)`` instead of selecting a
registered provider. Real custom-TTS use cases include: in-house voices,
on-prem synthesis, a tee that mirrors audio to a recorder, or a wrapper
that prepends an earcon.

Here we wrap the built-in OpenAI TTS with ``LoggingTTS``, which prints
each event on its way out.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart --group dev
  uv run easycat doctor
  uv run easycat doctor --env-file .env  # if keys live in .env
  uv run python examples/custom_tts_provider.py
  uv run --env-file .env python examples/custom_tts_provider.py  # if keys live in .env
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
from easycat.events import TTSEvent
from easycat.providers import TTSProvider
from easycat.tts.input import TTSInput
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig


class LoggingTTS:
    """Wraps any ``TTSProvider`` and prints each event on its way out.

    Implements the ``TTSProvider`` Protocol structurally — no base class.
    """

    def __init__(self, inner: TTSProvider) -> None:
        self._inner = inner

    @property
    def supports_ssml(self) -> bool:
        return self._inner.supports_ssml

    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        text = payload if isinstance(payload, str) else payload.text
        print(f"[tts] synthesize: {text[:80]!r}")
        async for event in self._inner.synthesize(payload):
            audio = getattr(event, "audio", None)
            size = len(audio.data) if audio is not None else 0
            print(f"[tts] {event.type.name:<8} {size} bytes")
            yield event

    async def stop(self) -> None:
        await self._inner.stop()

    async def cancel(self) -> None:
        await self._inner.cancel()

    def version_info(self) -> dict[str, str]:
        return {**self._inner.version_info(), "wrapper": "logging"}


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    inner_tts = OpenAITTS(OpenAITTSConfig(api_key=api_key))
    tts = LoggingTTS(inner_tts)

    config = EasyConfig.mic(
        openai_api_key=api_key,
        tts=tts,
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
