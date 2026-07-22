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
  uv run easycat doctor --env-file .env --json  # for parseable checks
  uv run python examples/custom_tts_provider.py
  uv run --env-file .env python examples/custom_tts_provider.py  # if keys live in .env
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from easycat import EasyConfig, require_env, run
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


def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    tts = LoggingTTS(OpenAITTS(OpenAITTSConfig(api_key=api_key)))
    run(
        EasyConfig.mic(
            tts=tts,
            agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
        )
    )


if __name__ == "__main__":
    main()
