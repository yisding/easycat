"""Tests for WS4 TurnManager: state machine, push-to-talk, barge-in, pre-roll."""

import asyncio

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
from easycat.turn_manager import (
    TurnManager,
    TurnManagerConfig,
    TurnManagerState,
    TurnMode,
)


def _chunk(n_bytes: int = 640, value: int = 0) -> AudioChunk:
    """Create a PCM16 16kHz chunk. 640 bytes = 320 samples = 20ms."""
    return AudioChunk(data=bytes([value & 0xFF] * n_bytes), format=PCM16_MONO_16K)


class EventCollector:
    """Collect events from EventBus for assertions."""

    def __init__(self, event_bus: EventBus) -> None:
        self.events: list[Event] = []
        for et in [
            TurnStarted,
            TurnEnded,
            BotStartedSpeaking,
            BotStoppedSpeaking,
            Interruption,
        ]:
            event_bus.subscribe(et, lambda e: self.events.append(e))

    @property
    def type_names(self) -> list[str]:
        return [type(e).__name__ for e in self.events]


# ── State machine transition tests ──────────────────────────────────


@pytest.mark.asyncio
async def test_initial_state_is_idle():
    """TurnManager starts in IDLE state."""
    bus = EventBus()
    tm = TurnManager(bus)
    assert tm.state == TurnManagerState.IDLE


@pytest.mark.asyncio
async def test_vad_start_transitions_to_user_speaking():
    """VADStartSpeaking should transition from Idle to UserSpeaking."""
    bus = EventBus()
    tm = TurnManager(bus)
    collector = EventCollector(bus)

    await tm.on_vad_event(VADStartSpeaking())

    assert tm.state == TurnManagerState.USER_SPEAKING
    assert "TurnStarted" in collector.type_names


@pytest.mark.asyncio
async def test_vad_stop_transitions_to_user_paused():
    """VADStopSpeaking should transition from UserSpeaking to UserPaused."""
    bus = EventBus()
    tm = TurnManager(bus)

    await tm.on_vad_event(VADStartSpeaking())
    assert tm.state == TurnManagerState.USER_SPEAKING

    await tm.on_vad_event(VADStopSpeaking())
    assert tm.state == TurnManagerState.USER_PAUSED


@pytest.mark.asyncio
async def test_silence_timeout_transitions_to_processing():
    """After silence timeout, should transition UserPaused -> Processing."""
    bus = EventBus()
    config = TurnManagerConfig(end_of_turn_silence_ms=50)  # Short for testing
    tm = TurnManager(bus, config=config)
    collector = EventCollector(bus)

    await tm.on_vad_event(VADStartSpeaking())
    await tm.on_vad_event(VADStopSpeaking())

    # Wait for silence timeout
    await asyncio.sleep(0.1)

    assert tm.state == TurnManagerState.PROCESSING
    assert "TurnEnded" in collector.type_names


@pytest.mark.asyncio
async def test_speech_resumes_cancels_timeout():
    """Speech resuming during UserPaused should cancel the silence timer."""
    bus = EventBus()
    config = TurnManagerConfig(end_of_turn_silence_ms=200)
    tm = TurnManager(bus, config=config)
    collector = EventCollector(bus)

    await tm.on_vad_event(VADStartSpeaking())
    await tm.on_vad_event(VADStopSpeaking())
    assert tm.state == TurnManagerState.USER_PAUSED

    # Resume speech before timeout
    await asyncio.sleep(0.05)
    await tm.on_vad_event(VADStartSpeaking())
    assert tm.state == TurnManagerState.USER_SPEAKING

    # Wait past the original timeout
    await asyncio.sleep(0.3)

    # Should NOT have transitioned to Processing
    assert "TurnEnded" not in collector.type_names


@pytest.mark.asyncio
async def test_full_turn_cycle():
    """Full cycle: Idle -> UserSpeaking -> UserPaused -> Processing -> BotSpeaking -> Idle."""
    bus = EventBus()
    config = TurnManagerConfig(end_of_turn_silence_ms=50)
    tm = TurnManager(bus, config=config)
    collector = EventCollector(bus)

    # User starts speaking
    await tm.on_vad_event(VADStartSpeaking())
    assert tm.state == TurnManagerState.USER_SPEAKING

    # User stops speaking
    await tm.on_vad_event(VADStopSpeaking())

    # Wait for silence timeout
    await asyncio.sleep(0.1)
    assert tm.state == TurnManagerState.PROCESSING

    # Bot starts speaking
    await tm.bot_started_speaking()
    assert tm.state == TurnManagerState.BOT_SPEAKING

    # Bot stops speaking
    await tm.bot_stopped_speaking()
    assert tm.state == TurnManagerState.IDLE

    assert "TurnStarted" in collector.type_names
    assert "TurnEnded" in collector.type_names
    assert "BotStartedSpeaking" in collector.type_names
    assert "BotStoppedSpeaking" in collector.type_names


# ── Pre-roll buffer tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_pre_roll_buffer_captures_audio_before_vad():
    """Pre-roll buffer should capture audio from before VAD trigger."""
    bus = EventBus()
    config = TurnManagerConfig(pre_roll_ms=100)
    tm = TurnManager(bus, config=config)

    # Feed audio frames before speech (simulating buffering)
    pre_chunks = [_chunk() for _ in range(5)]  # 5 x 20ms = 100ms
    for c in pre_chunks:
        tm.on_audio_frame(c)

    # VAD triggers speech start
    await tm.on_vad_event(VADStartSpeaking())

    # Turn audio should contain the pre-roll chunks
    assert len(tm.turn_audio) >= len(pre_chunks)
    # Pre-roll chunks should be the first frames in turn_audio
    for i, c in enumerate(pre_chunks):
        assert tm.turn_audio[i] is c


@pytest.mark.asyncio
async def test_pre_roll_buffer_trims_to_configured_duration():
    """Pre-roll buffer should not exceed configured duration."""
    bus = EventBus()
    config = TurnManagerConfig(pre_roll_ms=40)  # Only 40ms = 2 chunks of 20ms
    tm = TurnManager(bus, config=config)

    # Feed 10 chunks (200ms) before speech
    for _ in range(10):
        tm.on_audio_frame(_chunk())

    await tm.on_vad_event(VADStartSpeaking())

    # Should only have ~2 chunks of pre-roll (40ms / 20ms per chunk)
    assert len(tm.turn_audio) <= 3  # Allow 1 extra for boundary


@pytest.mark.asyncio
async def test_audio_captured_during_speech():
    """Audio frames during speech should be captured in turn_audio."""
    bus = EventBus()
    tm = TurnManager(bus)

    await tm.on_vad_event(VADStartSpeaking())

    speech_chunks = [_chunk(value=i) for i in range(5)]
    for c in speech_chunks:
        tm.on_audio_frame(c)

    assert len(tm.turn_audio) >= 5


# ── Push-to-talk tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_push_to_talk_start_turn():
    """Manual start_turn should transition Idle -> UserSpeaking."""
    bus = EventBus()
    config = TurnManagerConfig(mode=TurnMode.PUSH_TO_TALK)
    tm = TurnManager(bus, config=config)
    collector = EventCollector(bus)

    await tm.start_turn()
    assert tm.state == TurnManagerState.USER_SPEAKING
    assert "TurnStarted" in collector.type_names


@pytest.mark.asyncio
async def test_push_to_talk_end_turn():
    """Manual end_turn should transition UserSpeaking -> Processing."""
    bus = EventBus()
    config = TurnManagerConfig(mode=TurnMode.PUSH_TO_TALK)
    tm = TurnManager(bus, config=config)
    collector = EventCollector(bus)

    await tm.start_turn()
    await tm.end_turn()

    assert tm.state == TurnManagerState.PROCESSING
    assert "TurnEnded" in collector.type_names


@pytest.mark.asyncio
async def test_push_to_talk_ignores_vad_events():
    """In push-to-talk mode, VAD events should be ignored."""
    bus = EventBus()
    config = TurnManagerConfig(mode=TurnMode.PUSH_TO_TALK)
    tm = TurnManager(bus, config=config)
    collector = EventCollector(bus)

    await tm.on_vad_event(VADStartSpeaking())
    assert tm.state == TurnManagerState.IDLE
    assert "TurnStarted" not in collector.type_names


@pytest.mark.asyncio
async def test_push_to_talk_end_from_paused():
    """end_turn should also work from UserPaused state."""
    bus = EventBus()
    tm = TurnManager(bus)
    collector = EventCollector(bus)

    # Start turn via VAD, pause via VAD, then manually end
    await tm.on_vad_event(VADStartSpeaking())
    await tm.on_vad_event(VADStopSpeaking())
    assert tm.state == TurnManagerState.USER_PAUSED

    await tm.end_turn()
    assert tm.state == TurnManagerState.PROCESSING
    assert "TurnEnded" in collector.type_names


@pytest.mark.asyncio
async def test_mode_switching():
    """Switching modes at runtime should work."""
    bus = EventBus()
    tm = TurnManager(bus)

    assert tm.mode == TurnMode.VAD
    tm.set_mode(TurnMode.PUSH_TO_TALK)
    assert tm.mode == TurnMode.PUSH_TO_TALK
    tm.set_mode(TurnMode.VAD)
    assert tm.mode == TurnMode.VAD


# ── Barge-in / interruption tests ────────────────────────────────────


@pytest.mark.asyncio
async def test_barge_in_during_bot_speaking():
    """VAD start during BotSpeaking should trigger barge-in."""
    bus = EventBus()
    config = TurnManagerConfig(end_of_turn_silence_ms=50)
    cancel_called = [False]

    async def mock_cancel():
        cancel_called[0] = True
        await bus.emit(Interruption())  # Real callback emits Interruption

    tm = TurnManager(bus, config=config, cancel_turn_callback=mock_cancel)
    collector = EventCollector(bus)

    # Complete a turn to get to BotSpeaking
    await tm.on_vad_event(VADStartSpeaking())
    await tm.on_vad_event(VADStopSpeaking())
    await asyncio.sleep(0.1)  # Silence timeout
    await tm.bot_started_speaking()
    assert tm.state == TurnManagerState.BOT_SPEAKING

    # User barges in
    await tm.on_vad_event(VADStartSpeaking())

    # Cancel callback should have been called
    assert cancel_called[0]
    # Should have emitted Interruption + TurnStarted for new turn
    assert "Interruption" in collector.type_names
    # State should be UserSpeaking (new turn)
    assert tm.state == TurnManagerState.USER_SPEAKING
    # Count TurnStarted events (original + barge-in)
    turn_started_count = sum(1 for n in collector.type_names if n == "TurnStarted")
    assert turn_started_count == 2


@pytest.mark.asyncio
async def test_barge_in_starts_new_turn():
    """After barge-in, a new turn should be started with pre-roll."""
    bus = EventBus()
    cancel_called = [False]

    async def mock_cancel():
        cancel_called[0] = True
        await bus.emit(Interruption())  # Real callback emits Interruption

    tm = TurnManager(bus, cancel_turn_callback=mock_cancel)

    # Get to BotSpeaking
    tm._state = TurnManagerState.BOT_SPEAKING

    # Feed some audio for pre-roll
    for _ in range(3):
        tm.on_audio_frame(_chunk())

    # User barges in
    await tm.on_vad_event(VADStartSpeaking())

    assert tm.state == TurnManagerState.USER_SPEAKING
    assert cancel_called[0]
    # Pre-roll should be flushed into turn_audio
    assert len(tm.turn_audio) > 0


@pytest.mark.asyncio
async def test_barge_in_via_push_to_talk():
    """Manual start_turn during BotSpeaking should also trigger barge-in."""
    bus = EventBus()
    cancel_called = [False]

    async def mock_cancel():
        cancel_called[0] = True
        await bus.emit(Interruption())  # Real callback emits Interruption

    tm = TurnManager(bus, cancel_turn_callback=mock_cancel)
    collector = EventCollector(bus)

    tm._state = TurnManagerState.BOT_SPEAKING

    await tm.start_turn()

    assert cancel_called[0]
    assert "Interruption" in collector.type_names
    assert tm.state == TurnManagerState.USER_SPEAKING


# ── Reset / cleanup tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_returns_to_idle():
    """reset() should return to IDLE and clear buffers."""
    bus = EventBus()
    tm = TurnManager(bus)

    await tm.on_vad_event(VADStartSpeaking())
    tm.on_audio_frame(_chunk())
    assert tm.state == TurnManagerState.USER_SPEAKING
    assert len(tm.turn_audio) > 0

    tm.reset()

    assert tm.state == TurnManagerState.IDLE
    assert len(tm.turn_audio) == 0
    assert tm.cancel_token is None


@pytest.mark.asyncio
async def test_shutdown_cleans_up():
    """shutdown() should cancel timers and reset."""
    bus = EventBus()
    config = TurnManagerConfig(end_of_turn_silence_ms=5000)
    tm = TurnManager(bus, config=config)

    await tm.on_vad_event(VADStartSpeaking())
    await tm.on_vad_event(VADStopSpeaking())
    # Timer is running

    await tm.shutdown()
    assert tm.state == TurnManagerState.IDLE


# ── Cancel token tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_new_cancel_token_per_turn():
    """Each new turn should get a fresh cancel token."""
    bus = EventBus()
    config = TurnManagerConfig(end_of_turn_silence_ms=50)
    tm = TurnManager(bus, config=config)

    # First turn
    await tm.on_vad_event(VADStartSpeaking())
    token1 = tm.cancel_token
    assert token1 is not None

    await tm.on_vad_event(VADStopSpeaking())
    await asyncio.sleep(0.1)
    await tm.bot_started_speaking()
    await tm.bot_stopped_speaking()

    # Second turn
    await tm.on_vad_event(VADStartSpeaking())
    token2 = tm.cancel_token
    assert token2 is not None
    assert token1 is not token2


# ── Edge case tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vad_stop_ignored_when_not_speaking():
    """VADStopSpeaking when not in UserSpeaking should be ignored."""
    bus = EventBus()
    tm = TurnManager(bus)

    await tm.on_vad_event(VADStopSpeaking())
    assert tm.state == TurnManagerState.IDLE


@pytest.mark.asyncio
async def test_end_turn_ignored_when_idle():
    """end_turn when IDLE should be no-op."""
    bus = EventBus()
    tm = TurnManager(bus)
    collector = EventCollector(bus)

    await tm.end_turn()
    assert tm.state == TurnManagerState.IDLE
    assert "TurnEnded" not in collector.type_names


@pytest.mark.asyncio
async def test_start_turn_ignored_when_already_speaking():
    """start_turn when already UserSpeaking should be no-op."""
    bus = EventBus()
    tm = TurnManager(bus)

    await tm.on_vad_event(VADStartSpeaking())
    # Trying to start again should not change state or emit another event
    collector = EventCollector(bus)
    await tm.start_turn()
    assert tm.state == TurnManagerState.USER_SPEAKING
    assert "TurnStarted" not in collector.type_names


@pytest.mark.asyncio
async def test_bot_stopped_speaking_ignored_when_not_bot_speaking():
    """bot_stopped_speaking when not BotSpeaking should be no-op."""
    bus = EventBus()
    tm = TurnManager(bus)
    collector = EventCollector(bus)

    await tm.bot_stopped_speaking()
    assert tm.state == TurnManagerState.IDLE
    assert "BotStoppedSpeaking" not in collector.type_names
