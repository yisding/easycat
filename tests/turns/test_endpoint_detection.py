"""Tests for smart-turn integration."""

from __future__ import annotations

import asyncio

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import EventBus, TurnEnded, VADStartSpeaking, VADStopSpeaking
from easycat.smart_turn import SmartTurnResult
from easycat.turn_manager import TurnManager, TurnManagerConfig, TurnManagerState


def _chunk(n_bytes: int = 640) -> AudioChunk:
    """20 ms of PCM16 16 kHz silence."""
    return AudioChunk(data=bytes(n_bytes), format=PCM16_MONO_16K)


class FakeSmartTurn:
    """Fake detector returning configurable results."""

    def __init__(
        self,
        prediction: int = 1,
        probability: float = 0.95,
        delay: float = 0.0,
    ) -> None:
        self.prediction = prediction
        self.probability = probability
        self.delay = delay
        self.call_count = 0
        self.last_chunks: list[AudioChunk] | None = None

    async def detect(self, audio_chunks: list[AudioChunk]) -> SmartTurnResult:
        self.call_count += 1
        self.last_chunks = audio_chunks
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        return SmartTurnResult(
            prediction=self.prediction,
            probability=self.probability,
        )


class FailingSmartTurn:
    """Detector that always raises."""

    async def detect(self, audio_chunks: list[AudioChunk]) -> SmartTurnResult:
        raise RuntimeError("Model failed to load")


# ── Basic behavior ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_smart_turn_complete_ends_turn_immediately():
    """When detector says complete, turn ends without waiting full timeout."""
    bus = EventBus()
    detector = FakeSmartTurn(prediction=1, probability=0.9)
    config = TurnManagerConfig(
        end_of_turn_silence_ms=5000,
        endpoint_detector=detector,
    )
    tm = TurnManager(bus, config=config)

    events: list[TurnEnded] = []
    bus.subscribe(TurnEnded, lambda e: events.append(e))

    await tm.on_vad_event(VADStartSpeaking())
    for _ in range(5):
        tm.on_audio_frame(_chunk())
    await tm.on_vad_event(VADStopSpeaking())

    # Should end almost immediately (not 5000 ms)
    await asyncio.sleep(0.1)

    assert tm.state == TurnManagerState.PROCESSING
    assert len(events) == 1
    assert detector.call_count == 1


@pytest.mark.asyncio
async def test_smart_turn_incomplete_falls_back_to_timeout():
    """When detector says incomplete, falls back to silence timeout."""
    bus = EventBus()
    detector = FakeSmartTurn(prediction=0, probability=0.3)
    config = TurnManagerConfig(
        end_of_turn_silence_ms=100,
        endpoint_detector=detector,
    )
    tm = TurnManager(bus, config=config)

    events: list[TurnEnded] = []
    bus.subscribe(TurnEnded, lambda e: events.append(e))

    await tm.on_vad_event(VADStartSpeaking())
    for _ in range(3):
        tm.on_audio_frame(_chunk())
    await tm.on_vad_event(VADStopSpeaking())

    # Should NOT have ended immediately
    await asyncio.sleep(0.01)
    assert tm.state == TurnManagerState.USER_PAUSED
    assert len(events) == 0

    # But should end after the timeout
    await asyncio.sleep(0.15)
    assert tm.state == TurnManagerState.PROCESSING
    assert len(events) == 1


@pytest.mark.asyncio
async def test_no_detector_uses_normal_timeout():
    """Without a detector, behavior is unchanged."""
    bus = EventBus()
    config = TurnManagerConfig(end_of_turn_silence_ms=50)
    tm = TurnManager(bus, config=config)

    events: list[TurnEnded] = []
    bus.subscribe(TurnEnded, lambda e: events.append(e))

    await tm.on_vad_event(VADStartSpeaking())
    await tm.on_vad_event(VADStopSpeaking())

    await asyncio.sleep(0.1)
    assert tm.state == TurnManagerState.PROCESSING
    assert len(events) == 1


# ── Edge cases ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detector_error_falls_back_to_timeout():
    """If detector raises, fall back to normal timeout."""
    bus = EventBus()
    detector = FailingSmartTurn()
    config = TurnManagerConfig(
        end_of_turn_silence_ms=50,
        endpoint_detector=detector,
    )
    tm = TurnManager(bus, config=config)

    events: list[TurnEnded] = []
    bus.subscribe(TurnEnded, lambda e: events.append(e))

    await tm.on_vad_event(VADStartSpeaking())
    for _ in range(3):
        tm.on_audio_frame(_chunk())
    await tm.on_vad_event(VADStopSpeaking())

    await asyncio.sleep(0.1)
    assert tm.state == TurnManagerState.PROCESSING
    assert len(events) == 1


@pytest.mark.asyncio
async def test_speech_resumes_cancels_detector():
    """If speech resumes during detection, the timer is cancelled."""
    bus = EventBus()
    detector = FakeSmartTurn(prediction=1, probability=0.9, delay=0.3)
    config = TurnManagerConfig(
        end_of_turn_silence_ms=5000,
        endpoint_detector=detector,
    )
    tm = TurnManager(bus, config=config)

    events: list[TurnEnded] = []
    bus.subscribe(TurnEnded, lambda e: events.append(e))

    await tm.on_vad_event(VADStartSpeaking())
    for _ in range(3):
        tm.on_audio_frame(_chunk())
    await tm.on_vad_event(VADStopSpeaking())

    # Speech resumes before detector finishes
    await asyncio.sleep(0.05)
    await tm.on_vad_event(VADStartSpeaking())

    # Wait past detector delay
    await asyncio.sleep(0.5)

    # Should NOT have ended the turn
    assert tm.state == TurnManagerState.USER_SPEAKING
    assert len(events) == 0


@pytest.mark.asyncio
async def test_detector_receives_turn_audio():
    """Detector should receive the accumulated turn audio chunks."""
    bus = EventBus()
    detector = FakeSmartTurn(prediction=1, probability=0.9)
    config = TurnManagerConfig(
        end_of_turn_silence_ms=5000,
        endpoint_detector=detector,
    )
    tm = TurnManager(bus, config=config)

    await tm.on_vad_event(VADStartSpeaking())
    chunks = [_chunk() for _ in range(5)]
    for c in chunks:
        tm.on_audio_frame(c)
    await tm.on_vad_event(VADStopSpeaking())

    await asyncio.sleep(0.1)

    assert detector.last_chunks is not None
    assert len(detector.last_chunks) >= 5


@pytest.mark.asyncio
async def test_empty_audio_skips_detector():
    """If no audio accumulated, skip detector and use timeout."""
    bus = EventBus()
    detector = FakeSmartTurn(prediction=1, probability=0.9)
    config = TurnManagerConfig(
        end_of_turn_silence_ms=50,
        endpoint_detector=detector,
    )
    tm = TurnManager(bus, config=config)

    events: list[TurnEnded] = []
    bus.subscribe(TurnEnded, lambda e: events.append(e))

    await tm.on_vad_event(VADStartSpeaking())
    # No audio frames added
    await tm.on_vad_event(VADStopSpeaking())

    await asyncio.sleep(0.1)

    # Detector should not have been called
    assert detector.call_count == 0
    # Turn should still end via timeout
    assert len(events) == 1
