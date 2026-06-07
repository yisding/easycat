"""Inject a custom STT provider via EasyConfig.

``EasyConfig`` covers the common path: pick a registered provider by
name or config dataclass.  When you have your own provider -- an in-house
ASR, a wrapper that adds logging, a tee that mirrors audio to a recorder
-- pass the provider instance as ``EasyConfig.mic(stt=...)``.

This example wraps the built-in OpenAI realtime STT with a tiny
``LoggingSTT`` shim that prints every provider event, then wires it
into the standard mic session alongside the default OpenAI TTS provider.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart --group dev
  uv run easycat doctor
  uv run easycat doctor --env-file .env  # if keys live in .env
  uv run python examples/custom_stt_provider.py
  uv run --env-file .env python examples/custom_stt_provider.py  # if keys live in .env
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
from easycat.events import STTEvent
from easycat.providers import STTProvider
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTT, OpenAIRealtimeSTTConfig


class LoggingSTT:
    """Wraps any ``STTProvider`` and prints each event on its way out.

    A realistic custom-provider shape: delegation + instrumentation.
    Implements the ``STTProvider`` Protocol structurally — no base class
    to inherit from.
    """

    def __init__(self, inner: STTProvider) -> None:
        self._inner = inner

    async def start_stream(self) -> None:
        await self._inner.start_stream()

    async def send_audio(self, chunk: AudioChunk) -> None:
        await self._inner.send_audio(chunk)

    async def commit_segment(self) -> bool:
        return await self._inner.commit_segment()

    async def end_stream(self) -> None:
        await self._inner.end_stream()

    async def events(self) -> AsyncIterator[STTEvent]:
        async for event in self._inner.events():
            text = getattr(event, "text", "") or ""
            print(f"[stt] {event.type.name:<8} {text[:80]}")
            yield event

    def version_info(self) -> dict[str, str]:
        return {**self._inner.version_info(), "wrapper": "logging"}


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    inner_stt = OpenAIRealtimeSTT(
        OpenAIRealtimeSTTConfig(api_key=api_key),
    )
    stt = LoggingSTT(inner_stt)

    config = EasyConfig.mic(
        openai_api_key=api_key,
        stt=stt,
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
