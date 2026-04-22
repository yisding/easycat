"""Inject a custom VAD provider via SessionConfig.

The mirror of ``custom_stt_provider.py`` and ``custom_tts_provider.py``
for the VAD stage.  Real custom-VAD use cases include: a domain-tuned
speech detector, a tee that mirrors VAD events to a debug sink, or a
deterministic stub that fires speech start/stop on schedule for tests.

Here we wrap whichever VAD ``create_vad`` selects (Silero → FunASR →
TEN → Krisp) with ``LoggingVAD``, which prints each event.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv run python examples/custom_vad_provider.py
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
from easycat.events import Event, EventBus
from easycat.integrations.agents._factory import auto_adapt_agent
from easycat.providers import VADProvider
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTT, OpenAIRealtimeSTTConfig
from easycat.transports.local import LocalTransport, LocalTransportConfig
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig
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
        pre_roll_ms: int = 100,
        post_roll_ms: int = 100,
    ) -> None:
        self._inner.configure(
            min_speech_duration_ms=min_speech_duration_ms,
            min_silence_duration_ms=min_silence_duration_ms,
            sensitivity=sensitivity,
            pre_roll_ms=pre_roll_ms,
            post_roll_ms=post_roll_ms,
        )


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    event_bus = EventBus()
    stt = OpenAIRealtimeSTT(
        OpenAIRealtimeSTTConfig(api_key=api_key, event_bus=event_bus),
    )
    tts = OpenAITTS(OpenAITTSConfig(api_key=api_key))
    transport = LocalTransport(LocalTransportConfig())
    vad = LoggingVAD(create_vad(VADConfig()))

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
