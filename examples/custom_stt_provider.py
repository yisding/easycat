"""Inject a custom STT provider via SessionConfig.

``EasyCatConfig`` covers the common path: pick a registered provider by
name or config dataclass.  When you have your own provider — an in-house
ASR, a wrapper that adds logging, a tee that mirrors audio to a recorder
— drop down to ``SessionConfig`` and wire providers by hand.

This example wraps the built-in OpenAI realtime STT with a tiny
``LoggingSTT`` shim that prints every provider event, then wires it
into a ``Session`` alongside real OpenAI TTS and a local mic transport.
The agent runs through ``AgentRunner``, matching what ``create_session``
does internally for simple agents.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv run python examples/custom_stt_provider.py
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from easycat import (
    AgentRunner,
    AgentRunnerConfig,
    Session,
    SessionConfig,
    attach_runtime_feedback,
    require_env,
    wait_for_shutdown_signal,
)
from easycat.audio_format import AudioChunk
from easycat.events import EventBus, STTEvent
from easycat.integrations.agents._factory import auto_adapt_agent
from easycat.providers import STTProvider
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTT, OpenAIRealtimeSTTConfig
from easycat.transports.local import LocalTransport, LocalTransportConfig
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig
from easycat.vad import VADConfig, create_vad


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


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    # Build every provider by hand — no EasyCatConfig auto-wiring.
    event_bus = EventBus()
    inner_stt = OpenAIRealtimeSTT(
        OpenAIRealtimeSTTConfig(api_key=api_key, event_bus=event_bus),
    )
    stt = LoggingSTT(inner_stt)
    tts = OpenAITTS(OpenAITTSConfig(api_key=api_key))
    transport = LocalTransport(LocalTransportConfig())
    vad = create_vad(VADConfig())

    base_agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")
    agent = AgentRunner(auto_adapt_agent(base_agent), AgentRunnerConfig())

    config = SessionConfig(
        transport=transport,
        vad=vad,
        stt=stt,
        tts=tts,
        agent=agent,
        event_bus=event_bus,
    )
    session = Session(config)
    attach_runtime_feedback(session)

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
