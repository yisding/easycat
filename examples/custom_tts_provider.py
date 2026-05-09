"""Inject a custom TTS provider via SessionConfig.

The mirror of ``custom_stt_provider.py``: drop down to ``SessionConfig``
and supply a structurally-typed ``TTSProvider`` instead of letting
``EasyConfig`` auto-wire one.  Real custom-TTS use cases include:
in-house voices, on-prem synthesis, a tee that mirrors audio to a
recorder, or a wrapper that prepends an earcon.

Here we wrap the built-in OpenAI TTS with ``LoggingTTS``, which prints
each event on its way out.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart
  uv run python examples/custom_tts_provider.py
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
from easycat.events import EventBus, TTSEvent
from easycat.integrations.agents import auto_adapt_agent
from easycat.providers import TTSProvider
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTT, OpenAIRealtimeSTTConfig
from easycat.transports.local import LocalTransport, LocalTransportConfig
from easycat.tts.input import TTSInput
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig
from easycat.vad import VADConfig, create_vad


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


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    from agents import Agent  # type: ignore[import-untyped]

    event_bus = EventBus()
    stt = OpenAIRealtimeSTT(
        OpenAIRealtimeSTTConfig(api_key=api_key, event_bus=event_bus),
    )
    inner_tts = OpenAITTS(OpenAITTSConfig(api_key=api_key))
    tts = LoggingTTS(inner_tts)
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
