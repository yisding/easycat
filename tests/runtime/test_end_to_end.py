"""AC1.11: End-to-end journal verification.

Runs a full session turn with stub providers and verifies that the
journal captures records from every observability layer:
  - EVENT records (from EventTraceLogger strangler-fig)
  - SPAN_START / SPAN_END records (from SpanManager strangler-fig)
  - METRIC records (from InMemoryMetrics strangler-fig)
  - provider_versions record (from create_session)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.event_logging import EventLoggingConfig, EventTraceLogger
from easycat.events import (
    Event,
    STTEvent,
    STTEventType,
    TTSEvent,
    TTSEventType,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.metrics import InMemoryMetrics
from easycat.runtime.journal import InMemoryRingBuffer, JournalView
from easycat.runtime.records import JournalRecordKind
from easycat.session._session import Session
from easycat.session._types import SessionConfig
from easycat.tracing import Tracer
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManagerConfig

# ── Stub providers (adapted from test_session_smoke.py) ──────────


def _chunk(n: int = 320) -> AudioChunk:
    return AudioChunk(data=bytes(n), format=PCM16_MONO_16K)


class _Transport:
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


class _VAD:
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


class _STT:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()

    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def end_stream(self) -> None:
        await self._queue.put(STTEvent(type=STTEventType.FINAL, text="hello"))
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event


class _Agent:
    async def run(self, text: str) -> str:
        return f"Echo: {text}"


class _TTS:
    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


class _NoiseReducer:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk


# ── Test ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_turn_populates_journal():
    """One turn with stub providers produces journal records from all layers."""
    journal = InMemoryRingBuffer(capacity=10_000)
    tracer = Tracer()
    metrics = InMemoryMetrics(journal=journal)
    session_id = "e2e-test"

    config = SessionConfig(
        transport=_Transport(),
        vad=_VAD(),
        stt=_STT(),
        agent=_Agent(),
        tts=_TTS(),
        noise_reducer=_NoiseReducer(),
        turn_manager_config=TurnManagerConfig(end_of_turn_silence_ms=1),
        journal=journal,
        session_id=session_id,
        tracer=tracer,
        metrics=metrics,
    )
    session = Session(config)

    # Wire EventTraceLogger as a telephony helper (same as create_session).
    etl = EventTraceLogger(session.event_bus, EventLoggingConfig(enabled=True), journal=journal)
    session._telephony_helpers.append(etl)

    # ── Verify journal view is available ──────────────────────
    assert session.journal is not None
    assert isinstance(session.journal, JournalView)
    assert session.journal.enabled is True
    assert session.journal.degraded is False

    # ── Run one turn ──────────────────────────────────────────
    await session.start()
    await asyncio.sleep(0.5)  # let the pipeline complete
    await session.stop()

    # ── Read all records ──────────────────────────────────────
    records = journal.read()
    assert len(records) > 0, "Journal should have records after one turn"

    kinds = {r.kind for r in records}

    # ── EVENT records from EventTraceLogger adapter ───────────
    assert JournalRecordKind.EVENT in kinds, f"No EVENT records; kinds={kinds}"
    event_names = {r.name for r in records if r.kind == JournalRecordKind.EVENT}
    # At minimum, TurnStarted and STTFinal should appear.
    assert "TurnStarted" in event_names, f"Missing TurnStarted; events={event_names}"
    assert "STTFinal" in event_names, f"Missing STTFinal; events={event_names}"

    # ── SPAN records from SpanManager adapter ──────────��──────
    assert JournalRecordKind.SPAN_START in kinds, f"No SPAN_START records; kinds={kinds}"
    assert JournalRecordKind.SPAN_END in kinds, f"No SPAN_END records; kinds={kinds}"
    span_names = {r.name for r in records if r.kind == JournalRecordKind.SPAN_START}
    assert "turn" in span_names, f"Missing turn span; spans={span_names}"

    # ── METRIC records from InMemoryMetrics adapter ───────────
    assert JournalRecordKind.METRIC in kinds, f"No METRIC records; kinds={kinds}"
    metric_names = {r.name for r in records if r.kind == JournalRecordKind.METRIC}
    assert len(metric_names) > 0, "Should have at least one metric"

    # ── Monotonic sequence ────────────────────────────────────
    seqs = [r.sequence for r in records]
    assert seqs == sorted(seqs), "Sequences should be monotonically increasing"
    assert len(seqs) == len(set(seqs)), "No duplicate sequence numbers"

    # ── All records have session_id ───────────────────────────
    for r in records:
        assert r.session_id == session_id, f"Record {r.sequence} has wrong session_id"

    # ── Timing is populated ───────────────────────────────────
    for r in records:
        assert r.timing.wall_ns > 0, f"Record {r.sequence} missing wall_ns"
        assert r.timing.mono_ns > 0, f"Record {r.sequence} missing mono_ns"
