"""End-to-end turn pipeline integration tests.

Verifies the full audio processing pipeline:
  Noise Reduction -> VAD -> Turn-Taking

Tests:
  1. Noise reduction runs first, VAD receives cleaned audio
  2. Turn boundaries are detected correctly
  3. Barge-in scenario works end-to-end
  4. Pre-roll buffering works across pipeline stages
  5. Pipeline with passthrough noise reducer (auto fallback)
"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import (
    BotStartedSpeaking,
    BotStoppedSpeaking,
    Event,
    EventBus,
    Interruption,
    TurnEnded,
    TurnStarted,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.noise_reduction import PassthroughNoiseReducer
from easycat.turn_manager import TurnManager, TurnManagerConfig, TurnManagerState, TurnMode


def _chunk(n_bytes: int = 640) -> AudioChunk:
    """20ms of PCM16 16kHz silence."""
    return AudioChunk(data=bytes(n_bytes), format=PCM16_MONO_16K)


# ── Fake providers for integration testing ───────────────────────────


class FakeNoiseReducer:
    """Tracks that process was called, passes audio through."""

    def __init__(self) -> None:
        self.processed_count = 0

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        self.processed_count += 1
        return chunk


class FakeVADForIntegration:
    """VAD that follows a scripted sequence of speech/silence events."""

    def __init__(self, script: list[str]) -> None:
        """script: list of "speech", "silence", "none" per chunk."""
        self._script = script
        self._idx = 0

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        if self._idx < len(self._script):
            action = self._script[self._idx]
            self._idx += 1
            if action == "speech":
                yield VADStartSpeaking()
            elif action == "silence":
                yield VADStopSpeaking()
            # "none" yields nothing

    def configure(self, **kwargs: object) -> None:
        pass


# ── Integration test: noise reduction -> VAD -> turn-taking ─────────


@pytest.mark.asyncio
async def test_pipeline_noise_reduction_before_vad():
    """Noise reduction should run before VAD in the pipeline."""
    nr = FakeNoiseReducer()
    vad_received_chunks: list[AudioChunk] = []

    class TrackingVAD:
        async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
            vad_received_chunks.append(chunk)
            return
            yield  # async generator

        def configure(self, **kwargs: object) -> None:
            pass

    tracking_vad = TrackingVAD()

    # Simulate pipeline: audio -> noise reduction -> VAD
    chunks = [_chunk() for _ in range(5)]
    for chunk in chunks:
        cleaned = await nr.process(chunk)
        async for _ in tracking_vad.process(cleaned):
            pass

    assert nr.processed_count == 5
    assert len(vad_received_chunks) == 5


@pytest.mark.asyncio
async def test_pipeline_turn_boundaries():
    """VAD events should drive correct turn boundaries via TurnManager."""
    bus = EventBus()
    config = TurnManagerConfig(end_of_turn_silence_ms=50)
    tm = TurnManager(bus, config=config)

    events_collected: list[Event] = []
    for et in [TurnStarted, TurnEnded, VADStartSpeaking, VADStopSpeaking]:
        bus.subscribe(et, lambda e: events_collected.append(e))

    # Script: speech start, 2x nothing, speech stop
    vad = FakeVADForIntegration(["speech", "none", "none", "silence"])
    nr = PassthroughNoiseReducer()

    chunks = [_chunk() for _ in range(4)]
    for chunk in chunks:
        cleaned = await nr.process(chunk)
        async for vad_event in vad.process(cleaned):
            await bus.emit(vad_event)
            await tm.on_vad_event(vad_event)
        tm.on_audio_frame(cleaned)

    # Wait for silence timeout
    await asyncio.sleep(0.1)

    type_names = [type(e).__name__ for e in events_collected]
    assert "VADStartSpeaking" in type_names
    assert "TurnStarted" in type_names
    assert "VADStopSpeaking" in type_names
    assert "TurnEnded" in type_names

    # Verify ordering
    vs_idx = type_names.index("VADStartSpeaking")
    ts_idx = type_names.index("TurnStarted")
    ve_idx = type_names.index("VADStopSpeaking")
    te_idx = type_names.index("TurnEnded")
    assert vs_idx <= ts_idx
    assert ts_idx < ve_idx
    assert ve_idx < te_idx


@pytest.mark.asyncio
async def test_pipeline_barge_in_scenario():
    """Barge-in: user speaks while bot is playing -> cancel + new turn."""
    bus = EventBus()
    config = TurnManagerConfig(end_of_turn_silence_ms=50)

    cancel_called = [False]

    async def mock_cancel():
        cancel_called[0] = True
        await bus.emit(Interruption())  # Real callback emits Interruption

    tm = TurnManager(bus, config=config, cancel_turn_callback=mock_cancel)

    events_collected: list[Event] = []
    for et in [TurnStarted, TurnEnded, Interruption, BotStartedSpeaking, BotStoppedSpeaking]:
        bus.subscribe(et, lambda e: events_collected.append(e))

    # Phase 1: Complete a normal turn
    vad_phase1 = FakeVADForIntegration(["speech", "none", "silence"])
    nr = PassthroughNoiseReducer()

    for _ in range(3):
        chunk = _chunk()
        cleaned = await nr.process(chunk)
        async for vad_event in vad_phase1.process(cleaned):
            await bus.emit(vad_event)
            await tm.on_vad_event(vad_event)
        tm.on_audio_frame(cleaned)

    await asyncio.sleep(0.1)  # Silence timeout -> Processing
    assert tm.state == TurnManagerState.PROCESSING

    # Bot starts speaking
    await tm.bot_started_speaking()
    assert tm.state == TurnManagerState.BOT_SPEAKING

    # Phase 2: User barges in during bot speech
    # Feed some pre-roll audio
    for _ in range(3):
        tm.on_audio_frame(_chunk())

    await tm.on_vad_event(VADStartSpeaking())

    # Verify barge-in results
    assert cancel_called[0], "Cancel callback should have been called"
    assert tm.state == TurnManagerState.USER_SPEAKING

    type_names = [type(e).__name__ for e in events_collected]
    assert "Interruption" in type_names
    # Should have 2 TurnStarted: original + barge-in
    assert type_names.count("TurnStarted") == 2


@pytest.mark.asyncio
async def test_pipeline_pre_roll_preserved():
    """Pre-roll audio should be captured when turn starts."""
    bus = EventBus()
    config = TurnManagerConfig(pre_roll_ms=60)  # 3 x 20ms chunks
    tm = TurnManager(bus, config=config)

    nr = PassthroughNoiseReducer()

    # Feed 5 silence chunks (pre-roll buffer fills up)
    pre_chunks = []
    for _ in range(5):
        chunk = _chunk()
        cleaned = await nr.process(chunk)
        tm.on_audio_frame(cleaned)
        pre_chunks.append(cleaned)

    # VAD triggers speech
    await tm.on_vad_event(VADStartSpeaking())

    # Turn audio should contain pre-roll chunks (last ~3 at 60ms)
    assert len(tm.turn_audio) >= 2  # At least some pre-roll
    assert len(tm.turn_audio) <= 4  # Not more than configured window


@pytest.mark.asyncio
async def test_pipeline_push_to_talk_integration():
    """Push-to-talk mode should work through the pipeline."""
    bus = EventBus()
    config = TurnManagerConfig(mode=TurnMode.PUSH_TO_TALK)
    tm = TurnManager(bus, config=config)

    events_collected: list[Event] = []
    for et in [TurnStarted, TurnEnded]:
        bus.subscribe(et, lambda e: events_collected.append(e))

    nr = PassthroughNoiseReducer()

    # VAD events should be ignored in push-to-talk mode
    await tm.on_vad_event(VADStartSpeaking())
    assert tm.state == TurnManagerState.IDLE

    # Manual start
    await tm.start_turn()
    assert tm.state == TurnManagerState.USER_SPEAKING

    # Feed some audio
    for _ in range(5):
        chunk = _chunk()
        cleaned = await nr.process(chunk)
        tm.on_audio_frame(cleaned)

    # Manual end
    await tm.end_turn()
    assert tm.state == TurnManagerState.PROCESSING

    type_names = [type(e).__name__ for e in events_collected]
    assert "TurnStarted" in type_names
    assert "TurnEnded" in type_names


@pytest.mark.asyncio
async def test_pipeline_multiple_turns():
    """Pipeline should handle multiple consecutive turns correctly."""
    bus = EventBus()
    config = TurnManagerConfig(end_of_turn_silence_ms=50)
    tm = TurnManager(bus, config=config)

    events_collected: list[Event] = []
    for et in [TurnStarted, TurnEnded, BotStartedSpeaking, BotStoppedSpeaking]:
        bus.subscribe(et, lambda e: events_collected.append(e))

    # Turn 1
    await tm.on_vad_event(VADStartSpeaking())
    await tm.on_vad_event(VADStopSpeaking())
    await asyncio.sleep(0.1)
    await tm.bot_started_speaking()
    await tm.bot_stopped_speaking()
    assert tm.state == TurnManagerState.IDLE

    # Turn 2
    await tm.on_vad_event(VADStartSpeaking())
    await tm.on_vad_event(VADStopSpeaking())
    await asyncio.sleep(0.1)
    await tm.bot_started_speaking()
    await tm.bot_stopped_speaking()
    assert tm.state == TurnManagerState.IDLE

    type_names = [type(e).__name__ for e in events_collected]
    assert type_names.count("TurnStarted") == 2
    assert type_names.count("TurnEnded") == 2
    assert type_names.count("BotStartedSpeaking") == 2
    assert type_names.count("BotStoppedSpeaking") == 2


@pytest.mark.asyncio
async def test_pipeline_passthrough_nr_with_vad():
    """Passthrough noise reducer should not affect VAD detection."""
    nr = PassthroughNoiseReducer()
    bus = EventBus()
    config = TurnManagerConfig(end_of_turn_silence_ms=50)
    tm = TurnManager(bus, config=config)

    events_collected: list[Event] = []
    bus.subscribe(TurnStarted, lambda e: events_collected.append(e))
    bus.subscribe(TurnEnded, lambda e: events_collected.append(e))

    vad = FakeVADForIntegration(["speech", "silence"])

    for _ in range(2):
        chunk = _chunk()
        cleaned = await nr.process(chunk)
        assert cleaned is chunk  # Passthrough returns same object
        async for vad_event in vad.process(cleaned):
            await tm.on_vad_event(vad_event)
        tm.on_audio_frame(cleaned)

    await asyncio.sleep(0.1)

    type_names = [type(e).__name__ for e in events_collected]
    assert "TurnStarted" in type_names
    assert "TurnEnded" in type_names
