"""Tests for TTSSynthesizer — shared TTS synthesis logic."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat._span_manager import SpanManager
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.bounded_queue import BoundedAudioQueue, DropPolicy
from easycat.cancel import CancelToken
from easycat.events import EventBus, TTSAudio, TTSEvent, TTSEventType, TTSMarkers
from easycat.metrics import TTS_TTFB, TURN_E2E, InMemoryMetrics
from easycat.tracing import InMemoryTraceExporter, SpanStatus, Tracer
from easycat.tts_synthesizer import TTSSynthesizer

# ── Test helpers ───────────────────────────────────────────────────


def _chunk(n: int = 320) -> AudioChunk:
    return AudioChunk(data=bytes(n), format=PCM16_MONO_16K)


class FakeTTS:
    """TTS that yields one audio chunk per synthesize call."""

    def __init__(self, chunks: int = 1) -> None:
        self._chunks = chunks
        self.synthesized: list[str] = []
        self.cancelled = False

    async def synthesize(self, text: str) -> AsyncIterator[TTSEvent]:
        self.synthesized.append(text)
        for _ in range(self._chunks):
            yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())

    async def cancel(self) -> None:
        self.cancelled = True


class MarkerTTS:
    """TTS that yields audio then markers."""

    async def synthesize(self, text: str) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())
        yield TTSEvent(type=TTSEventType.MARKERS, markers=[{"word": "hello", "time": 0.1}])

    async def cancel(self) -> None:
        pass


class SlowTTS:
    """TTS that yields audio slowly."""

    async def synthesize(self, text: str) -> AsyncIterator[TTSEvent]:
        for _ in range(5):
            await asyncio.sleep(0.02)
            yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())

    async def cancel(self) -> None:
        pass


class FailingTTS:
    """TTS that raises mid-stream."""

    async def synthesize(self, text: str) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())
        raise RuntimeError("TTS failed")

    async def cancel(self) -> None:
        pass


def _make_synth(
    tts=None,
    metrics=None,
    tracer=None,
    timeout_config=None,
) -> tuple[TTSSynthesizer, EventBus, BoundedAudioQueue]:
    event_bus = EventBus()
    queue = BoundedAudioQueue(max_size=100, policy=DropPolicy.DROP_OLDEST, name="test")
    spans = SpanManager(tracer)
    synth = TTSSynthesizer(
        tts=tts or FakeTTS(),
        event_bus=event_bus,
        outbound_queue=queue,
        spans=spans,
        metrics=metrics,
        timeout_config=timeout_config,
    )
    return synth, event_bus, queue


# ── Basic synthesis tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_emits_tts_audio_event():
    synth, event_bus, _ = _make_synth()
    received: list[TTSAudio] = []
    event_bus.subscribe(TTSAudio, lambda e: received.append(e))

    result = await synth.synthesize("hello", None)

    assert result.audio_produced
    assert result.first_audio_time is not None
    assert len(received) == 1


@pytest.mark.asyncio
async def test_synthesize_queues_audio():
    synth, _, queue = _make_synth(tts=FakeTTS(chunks=3))

    result = await synth.synthesize("hello", None)

    assert result.audio_produced
    assert not queue.empty()


@pytest.mark.asyncio
async def test_synthesize_emits_markers():
    synth, event_bus, _ = _make_synth(tts=MarkerTTS())
    markers: list[TTSMarkers] = []
    event_bus.subscribe(TTSMarkers, lambda e: markers.append(e))

    await synth.synthesize("hello", None)

    assert len(markers) == 1
    assert markers[0].markers[0]["word"] == "hello"


@pytest.mark.asyncio
async def test_synthesize_no_audio_returns_false():
    class EmptyTTS:
        async def synthesize(self, text: str) -> AsyncIterator[TTSEvent]:
            return
            yield  # make it an async generator

        async def cancel(self) -> None:
            pass

    synth, _, _ = _make_synth(tts=EmptyTTS())
    result = await synth.synthesize("hello", None)
    assert not result.audio_produced
    assert result.first_audio_time is None


# ── Cancellation tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_stops_on_cancel_token():
    token = CancelToken()
    tts = SlowTTS()
    synth, event_bus, _ = _make_synth(tts=tts)

    received: list[TTSAudio] = []
    event_bus.subscribe(TTSAudio, lambda e: received.append(e))

    # Cancel after a short delay
    async def cancel_later():
        await asyncio.sleep(0.03)
        token.cancel()

    asyncio.create_task(cancel_later())
    await synth.synthesize("hello", token)

    # Should have gotten some but not all 5 chunks
    assert len(received) < 5


@pytest.mark.asyncio
async def test_synthesize_stops_on_is_active_false():
    active = True
    synth, event_bus, _ = _make_synth(tts=SlowTTS())

    received: list[TTSAudio] = []
    event_bus.subscribe(TTSAudio, lambda e: received.append(e))

    async def deactivate_later():
        await asyncio.sleep(0.03)
        nonlocal active
        active = False

    asyncio.create_task(deactivate_later())
    await synth.synthesize("hello", None, is_active=lambda: active)

    assert len(received) < 5


# ── Metrics tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_records_ttfb_metric():
    metrics = InMemoryMetrics()
    synth, _, _ = _make_synth(metrics=metrics)

    await synth.synthesize("hello", None)

    stats = metrics.get_latency(TTS_TTFB)
    assert stats.count == 1
    assert stats.min_ms > 0


@pytest.mark.asyncio
async def test_synthesize_records_e2e_metric():
    import time

    metrics = InMemoryMetrics()
    synth, _, _ = _make_synth(metrics=metrics)

    turn_end = time.monotonic() - 0.1  # simulate 100ms ago
    await synth.synthesize("hello", None, turn_end_time=turn_end)

    stats = metrics.get_latency(TURN_E2E)
    assert stats.count == 1
    assert stats.min_ms >= 100  # at least 100ms


@pytest.mark.asyncio
async def test_synthesize_no_e2e_without_turn_end_time():
    metrics = InMemoryMetrics()
    synth, _, _ = _make_synth(metrics=metrics)

    await synth.synthesize("hello", None)

    stats = metrics.get_latency(TURN_E2E)
    assert stats.count == 0


# ── Span management tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_creates_and_finishes_tts_span():
    exporter = InMemoryTraceExporter()
    tracer = Tracer(exporter)
    synth, _, _ = _make_synth(tracer=tracer)

    # Need a turn context for spans to work
    synth._spans.begin_turn()
    await synth.synthesize("hello", None)

    tts_spans = exporter.get_spans_by_name("tts")
    assert len(tts_spans) == 1
    assert tts_spans[0].status == SpanStatus.OK


@pytest.mark.asyncio
async def test_synthesize_marks_span_error_on_exception():
    exporter = InMemoryTraceExporter()
    tracer = Tracer(exporter)
    synth, _, _ = _make_synth(tts=FailingTTS(), tracer=tracer)

    synth._spans.begin_turn()
    with pytest.raises(RuntimeError, match="TTS failed"):
        await synth.synthesize("hello", None)

    tts_spans = exporter.get_spans_by_name("tts")
    assert len(tts_spans) == 1
    assert tts_spans[0].status == SpanStatus.ERROR


# ── Cancel method test ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_delegates_to_tts():
    tts = FakeTTS()
    synth, _, _ = _make_synth(tts=tts)

    await synth.cancel()
    assert tts.cancelled
