"""Task 1.10: End-to-end smoke test with stub providers.

Runs a full session lifecycle:
  start -> feed audio -> stub VAD triggers -> stub STT returns text ->
  stub agent returns text -> stub TTS returns audio -> audio out -> stop

Verifies:
  - All expected events fire in correct order
  - Session state transitions are correct
"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import (
    AgentDelta,
    AgentFinal,
    AudioIn,
    Event,
    STTFinal,
    TTSAudio,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.session import Session, SessionConfig, TurnState

# ── Stub providers for smoke test ──────────────────────────────────


def _chunk(n: int = 320) -> AudioChunk:
    return AudioChunk(data=bytes(n), format=PCM16_MONO_16K)


class SmokeTransport:
    """Yields 3 audio chunks then stops."""

    def __init__(self) -> None:
        self.sent: list[AudioChunk] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        for _ in range(3):
            yield _chunk()

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent.append(chunk)


class SmokeVAD:
    """Emits start on chunk 1, nothing on chunk 2, stop on chunk 3."""

    def __init__(self) -> None:
        self._n = 0

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        self._n += 1
        if self._n == 1:
            yield VADStartSpeaking()
        elif self._n == 3:
            yield VADStopSpeaking()

    def configure(self, **kwargs: object) -> None:
        pass


class SmokeSTT:
    """Queues final transcript on end_stream."""

    def __init__(self) -> None:
        self._q: asyncio.Queue[Event | None] = asyncio.Queue()

    async def start_stream(self) -> AsyncIterator[Event]:
        while True:
            ev = await self._q.get()
            if ev is None:
                break
            yield ev

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def end_stream(self) -> None:
        await self._q.put(STTFinal(text="good morning"))
        await self._q.put(None)


class SmokeAgent:
    async def run(self, text: str) -> str:
        return f"Reply to: {text}"


class SmokeTTS:
    async def synthesize(self, text: str) -> AsyncIterator[AudioChunk]:
        yield _chunk()
        yield _chunk()

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


class SmokeNoiseReducer:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk


# ── Smoke test ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_session_smoke():
    """Full end-to-end session lifecycle with stub providers."""
    transport = SmokeTransport()
    config = SessionConfig(
        transport=transport,
        vad=SmokeVAD(),
        stt=SmokeSTT(),
        agent=SmokeAgent(),
        tts=SmokeTTS(),
        noise_reducer=SmokeNoiseReducer(),
    )
    session = Session(config)

    # Collect all events in order
    timeline: list[Event] = []

    for event_type in [
        AudioIn,
        VADStartSpeaking,
        VADStopSpeaking,
        STTFinal,
        AgentDelta,
        AgentFinal,
        TTSAudio,
    ]:
        session.event_bus.subscribe(event_type, lambda e: timeline.append(e))

    # ── Run session ────────────────────────────────────────────
    assert session.turn_state == TurnState.IDLE

    await session.start()
    assert session.is_running

    # Wait for pipeline to complete
    await asyncio.sleep(0.3)
    await session.stop()

    # ── Verify event sequence ──────────────────────────────────
    type_names = [type(e).__name__ for e in timeline]

    # All expected events must appear
    assert "AudioIn" in type_names, f"Missing AudioIn in {type_names}"
    assert "VADStartSpeaking" in type_names, f"Missing VADStartSpeaking in {type_names}"
    assert "VADStopSpeaking" in type_names, f"Missing VADStopSpeaking in {type_names}"
    assert "STTFinal" in type_names, f"Missing STTFinal in {type_names}"
    assert "AgentDelta" in type_names, f"Missing AgentDelta in {type_names}"
    assert "AgentFinal" in type_names, f"Missing AgentFinal in {type_names}"
    assert "TTSAudio" in type_names, f"Missing TTSAudio in {type_names}"

    # Verify ordering: AudioIn before VADStart, VADStart before VADStop, etc.
    ai_idx = type_names.index("AudioIn")
    vs_idx = type_names.index("VADStartSpeaking")
    ve_idx = type_names.index("VADStopSpeaking")
    sf_idx = type_names.index("STTFinal")
    af_idx = type_names.index("AgentFinal")
    ta_idx = type_names.index("TTSAudio")

    assert ai_idx < vs_idx, "AudioIn should come before VADStartSpeaking"
    assert vs_idx < ve_idx, "VADStartSpeaking should come before VADStopSpeaking"
    assert ve_idx < sf_idx, "VADStopSpeaking should come before STTFinal"
    assert sf_idx < af_idx, "STTFinal should come before AgentFinal"
    assert af_idx < ta_idx, "AgentFinal should come before TTSAudio"

    # ── Verify content ─────────────────────────────────────────
    stt_finals = [e for e in timeline if isinstance(e, STTFinal)]
    assert stt_finals[0].text == "good morning"

    agent_finals = [e for e in timeline if isinstance(e, AgentFinal)]
    assert agent_finals[0].text == "Reply to: good morning"

    # ── Verify transport received TTS output ───────────────────
    assert len(transport.sent) == 2  # SmokeTTS yields 2 chunks

    # ── Verify final state ─────────────────────────────────────
    assert not session.is_running
    assert session.turn_state == TurnState.IDLE
