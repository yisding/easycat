"""TurnManager tests: state machine, push-to-talk, barge-in, pre-roll."""

import asyncio
import contextlib
import logging
import math
from unittest.mock import AsyncMock

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.cancel import CancelToken
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
from easycat.smart_turn import SmartTurnResult
from easycat.turn_manager import (
    TurnManager,
    TurnManagerConfig,
    TurnManagerState,
    TurnMode,
)
from easycat.vad._base import _VADBase
from easycat.vad.factory import VADConfig


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


@pytest.mark.parametrize(
    "field",
    [
        "end_of_turn_silence_ms",
        "punctuated_end_of_turn_silence_ms",
        "stt_segment_silence_ms",
        "pre_roll_ms",
        "max_turn_audio_ms",
    ],
)
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_turn_manager_config_rejects_nonfinite_time_limits(field: str, value: float) -> None:
    with pytest.raises(ValueError, match=field):
        TurnManagerConfig(**{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "end_of_turn_silence_ms",
        "punctuated_end_of_turn_silence_ms",
        "stt_segment_silence_ms",
        "pre_roll_ms",
        "max_turn_audio_ms",
    ],
)
def test_turn_manager_config_rejects_boolean_time_limits(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        TurnManagerConfig(**{field: True})


@pytest.mark.parametrize("field", ["max_pre_roll_chunks", "max_turn_audio_chunks"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_turn_manager_config_requires_positive_integral_chunk_limits(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        TurnManagerConfig(**{field: value})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("completion_path", "expected_reason"),
    [
        ("manual", "manual_end"),
        ("silence", "silence_timeout"),
        ("smart_turn", "smart_turn_complete"),
    ],
)
async def test_user_turn_completion_emits_one_correlated_event_with_reason(
    completion_path: str,
    expected_reason: str,
) -> None:
    class CompleteDetector:
        async def detect(self, _audio_chunks: list[AudioChunk]) -> SmartTurnResult:
            return SmartTurnResult(prediction=1, probability=0.9)

    bus = EventBus()
    ended: list[TurnEnded] = []
    started: list[TurnStarted] = []
    reasons: list[str] = []
    bus.subscribe(TurnStarted, started.append)
    bus.subscribe(TurnEnded, ended.append)
    config = TurnManagerConfig(
        end_of_turn_silence_ms=0,
        endpoint_detector=CompleteDetector() if completion_path == "smart_turn" else None,
    )
    manager = TurnManager(bus, config=config)
    manager.bind_session("session-completion")
    manager.bind_journal_hook(lambda _old, _new, reason, _turn_id: reasons.append(reason))

    await manager.on_vad_event(VADStartSpeaking())
    if completion_path == "manual":
        await manager.end_turn()
    else:
        if completion_path == "smart_turn":
            manager.on_audio_frame(_chunk())
        await manager.on_vad_event(VADStopSpeaking())
        timer = manager._silence_timer_task
        assert timer is not None
        await timer

    assert manager.state == TurnManagerState.PROCESSING
    assert reasons[-1] == expected_reason
    assert len(started) == 1
    assert len(ended) == 1
    assert ended[0].session_id == started[0].session_id == "session-completion"
    assert ended[0].turn_id == started[0].turn_id


@pytest.mark.asyncio
async def test_stale_smart_turn_timer_cannot_end_a_later_pause() -> None:
    """A detector that suppresses cancellation must not affect a new pause."""

    class CancellationResistantDetector:
        def __init__(self) -> None:
            self.started = [asyncio.Event(), asyncio.Event()]
            self.release = [asyncio.Event(), asyncio.Event()]
            self.cancel_seen = asyncio.Event()
            self.calls = 0

        async def detect(self, _audio: list[AudioChunk]) -> SmartTurnResult:
            index = self.calls
            self.calls += 1
            self.started[index].set()
            try:
                await self.release[index].wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                await self.release[index].wait()
            return SmartTurnResult(prediction=0, probability=0.0)

    detector = CancellationResistantDetector()
    manager = TurnManager(
        EventBus(),
        config=TurnManagerConfig(end_of_turn_silence_ms=0, endpoint_detector=detector),
    )
    try:
        await manager.on_vad_event(VADStartSpeaking())
        manager.on_audio_frame(_chunk())
        await manager.on_vad_event(VADStopSpeaking())
        await detector.started[0].wait()

        await manager.on_vad_event(VADStartSpeaking())
        await detector.cancel_seen.wait()
        await manager.on_vad_event(VADStopSpeaking())
        await detector.started[1].wait()

        detector.release[0].set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert manager.state == TurnManagerState.USER_PAUSED

        detector.release[1].set()
        timer = manager._silence_timer_task
        assert timer is not None
        await timer
    finally:
        for release in detector.release:
            release.set()
        await manager.shutdown()


@pytest.mark.asyncio
async def test_shutdown_reaps_replaced_cancellation_resistant_timer() -> None:
    class CancellationResistantDetector:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancel_seen = asyncio.Event()
            self.release = asyncio.Event()

        async def detect(self, _audio: list[AudioChunk]) -> SmartTurnResult:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                await self.release.wait()
            return SmartTurnResult(prediction=0, probability=0.0)

    detector = CancellationResistantDetector()
    manager = TurnManager(
        EventBus(),
        config=TurnManagerConfig(end_of_turn_silence_ms=0, endpoint_detector=detector),
    )
    await manager.on_vad_event(VADStartSpeaking())
    manager.on_audio_frame(_chunk())
    await manager.on_vad_event(VADStopSpeaking())
    await detector.started.wait()
    replaced = manager._silence_timer_task
    assert replaced is not None

    await manager.on_vad_event(VADStartSpeaking())
    await detector.cancel_seen.wait()
    assert manager._silence_timer_task is None
    assert not replaced.done()

    try:
        detector.release.set()
        await manager.shutdown()
        assert replaced.done()
        assert manager._silence_timer_tasks == set()
    finally:
        detector.release.set()
        await asyncio.gather(replaced, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_closes_silence_timer_admission_while_reaping() -> None:
    class CancellationResistantDetector:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancel_seen = asyncio.Event()
            self.release = asyncio.Event()

        async def detect(self, _audio: list[AudioChunk]) -> SmartTurnResult:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                await self.release.wait()
            return SmartTurnResult(prediction=0, probability=0.0)

    detector = CancellationResistantDetector()
    manager = TurnManager(
        EventBus(),
        config=TurnManagerConfig(end_of_turn_silence_ms=0, endpoint_detector=detector),
    )
    await manager.on_vad_event(VADStartSpeaking())
    manager.on_audio_frame(_chunk())
    await manager.on_vad_event(VADStopSpeaking())
    await detector.started.wait()

    shutdown = asyncio.create_task(manager.shutdown())
    await detector.cancel_seen.wait()
    await manager.on_vad_event(VADStartSpeaking())
    await manager.on_vad_event(VADStopSpeaking())
    detector.release.set()
    await shutdown

    assert manager._silence_timer_tasks == set()
    assert manager._silence_timer_task is None


@pytest.mark.asyncio
async def test_shutdown_closes_manual_and_application_turn_admission() -> None:
    bus = EventBus()
    events = EventCollector(bus)
    manager = TurnManager(bus)

    await manager.shutdown()
    await manager.start_turn()
    await manager.end_turn()

    with pytest.raises(RuntimeError, match="shutting down"):
        manager.begin_application_turn("late-application", CancelToken())

    assert manager.state is TurnManagerState.IDLE
    assert manager.cancel_token is None
    assert events.events == []


def test_default_endpointing_outlasts_vad_restart_confirmation() -> None:
    """The default turn grace must stay above the VAD restart-confirmation gate.

    On the plain-VAD path (no smart-turn) the only event that can cancel a
    pending fixed endpoint is a *confirmed* ``VADStartSpeaking``, which the
    default VAD emits only after ``min_speech_duration_ms`` of continuous
    resumed speech plus frame quantization.  If the grace period were at or
    below that gate, ordinary mid-sentence pauses would be split/truncated.
    """
    vad = VADConfig()
    turn = TurnManagerConfig()

    assert vad.min_silence_duration_ms == 50
    assert vad.min_speech_duration_ms == 250
    assert turn.end_of_turn_silence_ms == 500
    assert turn.punctuated_end_of_turn_silence_ms == 200
    # Leave the restart confirmation (250 ms gate + frame quantization)
    # real headroom before the endpoint fires.
    assert turn.end_of_turn_silence_ms >= vad.min_speech_duration_ms + 100


@pytest.mark.asyncio
async def test_confirmed_vad_restart_cancels_pending_endpoint() -> None:
    """Only a *confirmed* VAD restart cancels the pending fixed endpoint.

    The VAD is driven with simulated timestamps and the endpoint timer is set
    far in the future, so this is a deterministic event-path test (no
    wall-clock race): resumed speech is invisible to the TurnManager until
    the ``min_speech_duration_ms`` confirmation gate has elapsed, and the
    confirmed restart is what cancels the silence timer.
    """
    bus = EventBus()
    manager = TurnManager(bus, config=TurnManagerConfig(end_of_turn_silence_ms=60_000))
    config = VADConfig()
    vad = _VADBase()
    vad.configure(
        min_speech_duration_ms=config.min_speech_duration_ms,
        min_silence_duration_ms=config.min_silence_duration_ms,
    )

    async def evaluate(probability: float, now: float) -> None:
        for event in vad._evaluate_speech(probability, now):
            await manager.on_vad_event(event)

    try:
        await evaluate(1.0, 0.000)
        await evaluate(1.0, 0.251)
        assert manager.state == TurnManagerState.USER_SPEAKING

        await evaluate(0.0, 0.300)
        await evaluate(0.0, 0.351)
        assert manager.state == TurnManagerState.USER_PAUSED
        timer = manager._silence_timer_task
        assert timer is not None and not timer.done()

        # Resumed speech shorter than the confirmation gate stays invisible:
        # no VADStartSpeaking is emitted, so the endpoint is still pending.
        await evaluate(1.0, 0.400)
        await evaluate(1.0, 0.500)
        assert manager.state == TurnManagerState.USER_PAUSED
        assert manager._silence_timer_task is timer

        # Once the gate elapses, the confirmed restart cancels the endpoint.
        await evaluate(1.0, 0.651)
        assert manager.state == TurnManagerState.USER_SPEAKING
        assert manager._silence_timer_task is None
        # The timer task swallows CancelledError internally; awaiting it
        # proves the 60 s sleep was cancelled rather than run to completion.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(timer, timeout=1.0)
        assert timer.done()
    finally:
        await manager.shutdown()


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

    try:
        await tm.on_vad_event(VADStopSpeaking())
        assert tm.state == TurnManagerState.USER_PAUSED
    finally:
        await tm.shutdown()


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
async def test_silence_timeout_logs_strict_handler_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = EventBus(handler_error_policy="raise")
    tm = TurnManager(bus, config=TurnManagerConfig(end_of_turn_silence_ms=0))

    async def fail(_event: TurnEnded) -> None:
        raise RuntimeError("turn handler failed")

    bus.subscribe(TurnEnded, fail)
    with caplog.at_level(logging.ERROR, logger="easycat.runtime.scope"):
        await tm.on_vad_event(VADStartSpeaking())
        await tm.on_vad_event(VADStopSpeaking())
        timer = tm._silence_timer_task
        assert timer is not None
        with pytest.raises(RuntimeError, match="turn handler failed"):
            await asyncio.wait_for(timer, timeout=1)

    assert "Background task failed" in caplog.text
    assert "turn handler failed" in caplog.text


@pytest.mark.asyncio
async def test_bot_started_from_turn_ended_handler_does_not_cancel_dispatch():
    """bot_started_speaking must not cancel its own silence-timeout dispatch."""
    bus = EventBus()
    config = TurnManagerConfig(end_of_turn_silence_ms=10)
    tm = TurnManager(bus, config=config)
    lifecycle: list[str] = []
    second_turn_ended_ran = asyncio.Event()

    async def start_bot_from_turn_ended(event: TurnEnded) -> None:
        assert asyncio.current_task() is tm._silence_timer_task
        lifecycle.append("turn_ended_first_start")
        await tm.bot_started_speaking()
        lifecycle.append("turn_ended_first_done")

    async def later_turn_ended_handler(event: TurnEnded) -> None:
        lifecycle.append("turn_ended_second")
        second_turn_ended_ran.set()

    async def bot_started_handler(event: BotStartedSpeaking) -> None:
        lifecycle.append("bot_started")

    bus.subscribe(TurnEnded, start_bot_from_turn_ended)
    bus.subscribe(TurnEnded, later_turn_ended_handler)
    bus.subscribe(BotStartedSpeaking, bot_started_handler)

    await tm.on_vad_event(VADStartSpeaking())
    await tm.on_vad_event(VADStopSpeaking())

    await asyncio.wait_for(second_turn_ended_ran.wait(), timeout=1.0)

    assert lifecycle == [
        "turn_ended_first_start",
        "bot_started",
        "turn_ended_first_done",
        "turn_ended_second",
    ]
    assert tm.state == TurnManagerState.BOT_SPEAKING


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
async def test_punctuated_stt_final_shortens_fixed_endpoint_timeout() -> None:
    bus = EventBus()
    tm = TurnManager(
        bus,
        config=TurnManagerConfig(
            end_of_turn_silence_ms=120,
            punctuated_end_of_turn_silence_ms=20,
        ),
    )
    collector = EventCollector(bus)
    reasons: list[str] = []
    tm.bind_journal_hook(lambda _old, _new, reason, _turn_id: reasons.append(reason))

    await tm.on_vad_event(VADStartSpeaking())
    await tm.on_vad_event(VADStopSpeaking())
    tm.on_stt_final('That is complete."', pause=tm.capture_pause())
    await asyncio.sleep(0.05)

    assert tm.state == TurnManagerState.PROCESSING
    assert "TurnEnded" in collector.type_names
    assert reasons[-1] == "punctuated_silence_timeout"


@pytest.mark.asyncio
async def test_unpunctuated_stt_final_keeps_full_endpoint_timeout() -> None:
    bus = EventBus()
    tm = TurnManager(
        bus,
        config=TurnManagerConfig(
            end_of_turn_silence_ms=100,
            punctuated_end_of_turn_silence_ms=20,
        ),
    )

    await tm.on_vad_event(VADStartSpeaking())
    await tm.on_vad_event(VADStopSpeaking())
    tm.on_stt_final("still thinking", pause=tm.capture_pause())
    await asyncio.sleep(0.04)
    assert tm.state == TurnManagerState.USER_PAUSED

    await asyncio.sleep(0.09)
    assert tm.state == TurnManagerState.PROCESSING


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Wait...", "Wait…", 'Wait..."'])
async def test_ellipsis_does_not_shorten_endpoint_timeout(text: str) -> None:
    bus = EventBus()
    tm = TurnManager(
        bus,
        config=TurnManagerConfig(
            end_of_turn_silence_ms=100,
            punctuated_end_of_turn_silence_ms=20,
        ),
    )

    await tm.on_vad_event(VADStartSpeaking())
    await tm.on_vad_event(VADStopSpeaking())
    tm.on_stt_final(text, pause=tm.capture_pause())

    assert not tm._punctuated_transcript_event.is_set()
    await tm.shutdown()


@pytest.mark.asyncio
async def test_late_punctuated_final_uses_elapsed_pause_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = EventBus()
    tm = TurnManager(
        bus,
        config=TurnManagerConfig(
            end_of_turn_silence_ms=500,
            punctuated_end_of_turn_silence_ms=100,
        ),
    )

    await tm.on_vad_event(VADStartSpeaking())
    await tm.on_vad_event(VADStopSpeaking())
    silence_started = tm._silence_start_time
    assert silence_started is not None

    timer = tm._silence_timer_task
    assert timer is not None
    timer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await timer
    tm.on_stt_final("Complete.", pause=tm.capture_pause())

    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(
        "easycat.turn_manager.time.monotonic",
        lambda: silence_started + 0.2,
    )

    assert await tm._wait_for_fixed_endpoint() is True
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_misconfigured_punctuation_wait_logs_disabled_shortening(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tm = TurnManager(
        EventBus(),
        config=TurnManagerConfig(
            end_of_turn_silence_ms=100,
            punctuated_end_of_turn_silence_ms=100,
        ),
    )
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)

    with caplog.at_level(logging.DEBUG, logger="easycat.turn_manager"):
        assert await tm._wait_for_fixed_endpoint() is False

    sleep.assert_awaited_once_with(0.1)
    assert "Punctuation endpoint shortening disabled" in caplog.text


@pytest.mark.asyncio
async def test_none_punctuation_wait_disables_shortening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tm = TurnManager(
        EventBus(),
        config=TurnManagerConfig(
            end_of_turn_silence_ms=100,
            punctuated_end_of_turn_silence_ms=None,
        ),
    )
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)

    assert await tm._wait_for_fixed_endpoint() is False
    sleep.assert_awaited_once_with(0.1)


@pytest.mark.asyncio
async def test_stt_final_before_pause_does_not_shorten_next_pause() -> None:
    bus = EventBus()
    tm = TurnManager(
        bus,
        config=TurnManagerConfig(
            end_of_turn_silence_ms=100,
            punctuated_end_of_turn_silence_ms=20,
        ),
    )

    await tm.on_vad_event(VADStartSpeaking())
    tm.on_stt_final("Old segment.", pause=tm.capture_pause())
    await tm.on_vad_event(VADStopSpeaking())
    await asyncio.sleep(0.04)

    assert tm.state == TurnManagerState.USER_PAUSED
    await tm.shutdown()


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


def test_config_normalizes_serialized_turn_mode() -> None:
    config = TurnManagerConfig(mode="push_to_talk")  # type: ignore[arg-type]

    assert config.mode is TurnMode.PUSH_TO_TALK


def test_config_rejects_invalid_turn_mode() -> None:
    with pytest.raises(ValueError, match="Invalid mode"):
        TurnManagerConfig(mode="push-to-talk")  # type: ignore[arg-type]


def test_set_mode_rejects_invalid_value_without_mutating_mode() -> None:
    tm = TurnManager(EventBus(), config=TurnManagerConfig(mode=TurnMode.PUSH_TO_TALK))

    with pytest.raises(ValueError, match="Invalid mode"):
        tm.set_mode("manual")  # type: ignore[arg-type]

    assert tm.mode is TurnMode.PUSH_TO_TALK


@pytest.mark.asyncio
async def test_bot_started_ignores_user_paused_until_turn_ends():
    """bot_started_speaking must not bypass a paused user's TurnEnded path."""
    bus = EventBus()
    config = TurnManagerConfig(end_of_turn_silence_ms=50)
    tm = TurnManager(bus, config=config)
    collector = EventCollector(bus)

    await tm.on_vad_event(VADStartSpeaking())
    await tm.on_vad_event(VADStopSpeaking())

    assert tm.state == TurnManagerState.USER_PAUSED
    assert tm._silence_timer_task is not None
    assert not tm._silence_timer_task.done()

    await tm.bot_started_speaking()

    assert tm.state == TurnManagerState.USER_PAUSED
    assert "BotStartedSpeaking" not in collector.type_names
    assert tm._silence_timer_task is not None
    assert not tm._silence_timer_task.done()

    await asyncio.sleep(0.1)

    assert tm.state == TurnManagerState.PROCESSING
    assert "TurnEnded" in collector.type_names


@pytest.mark.asyncio
async def test_bot_started_cancels_stale_silence_timer_after_turn_completion():
    """bot_started_speaking should clear stale timers once no user turn is active."""
    bus = EventBus()
    tm = TurnManager(bus)
    stale_timer = asyncio.create_task(asyncio.sleep(10))
    tm._state = TurnManagerState.PROCESSING
    tm._silence_timer_task = stale_timer

    try:
        await tm.bot_started_speaking()

        assert tm.state == TurnManagerState.BOT_SPEAKING
        assert tm._silence_timer_task is None
        await asyncio.sleep(0)
        assert stale_timer.cancelled()
    finally:
        stale_timer.cancel()


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
async def test_concurrent_vad_and_manual_barge_ins_claim_one_successor_turn():
    """VAD and PTT cannot both publish a successor during an async cutoff."""
    bus = EventBus()
    cutoff_started = asyncio.Event()
    release_cutoff = asyncio.Event()
    cancel_calls = 0

    async def delayed_cancel() -> None:
        nonlocal cancel_calls
        cancel_calls += 1
        cutoff_started.set()
        await release_cutoff.wait()
        await bus.emit(Interruption())

    tm = TurnManager(bus, cancel_turn_callback=delayed_cancel)
    collector = EventCollector(bus)
    previous_token = CancelToken()
    tm._state = TurnManagerState.BOT_SPEAKING
    tm._current_turn_id = "old-turn"
    tm._cancel_token = previous_token

    vad_barge_in = asyncio.create_task(tm.on_vad_event(VADStartSpeaking()))
    await asyncio.wait_for(cutoff_started.wait(), timeout=0.25)
    manual_barge_in = asyncio.create_task(tm.start_turn())
    await asyncio.sleep(0)

    release_cutoff.set()
    await asyncio.wait_for(asyncio.gather(vad_barge_in, manual_barge_in), timeout=0.25)

    assert cancel_calls == 1
    assert previous_token.is_cancelled
    assert tm.state == TurnManagerState.USER_SPEAKING
    assert collector.type_names.count("Interruption") == 1
    assert collector.type_names.count("TurnStarted") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["vad_idle", "barge_in", "manual"])
async def test_begin_turn_shared_bookkeeping_across_paths(path):
    """All three turn-start paths share the consolidated ``_begin_turn`` logic.

    Each of VAD-IDLE, barge-in, and manual (push-to-talk) must: bump the turn
    counter by 1, mint a ``turn-NNNN-xxxxxxxx`` id, issue a fresh non-cancelled
    ``cancel_token``, flush pre-roll into ``turn_audio``, and emit exactly one
    ``TurnStarted``.
    """
    bus = EventBus()

    async def mock_cancel():
        await bus.emit(Interruption())  # Real callback emits Interruption

    tm = TurnManager(bus, cancel_turn_callback=mock_cancel)
    collector = EventCollector(bus)

    if path == "barge_in":
        tm._state = TurnManagerState.BOT_SPEAKING

    # Feed pre-roll audio that must land in turn_audio after the turn starts.
    for _ in range(3):
        tm.on_audio_frame(_chunk(value=7))

    counter_before = tm._turn_counter

    if path == "vad_idle" or path == "barge_in":
        await tm.on_vad_event(VADStartSpeaking())
    else:
        await tm.start_turn()

    assert tm.state == TurnManagerState.USER_SPEAKING
    # Counter bumped by exactly one and a well-formed turn id minted.
    assert tm._turn_counter == counter_before + 1
    assert tm._current_turn_id is not None
    assert tm._current_turn_id.startswith(f"turn-{tm._turn_counter:04d}-")
    assert len(tm._current_turn_id.split("-")[-1]) == 8
    # Fresh, non-cancelled token issued for the new turn.
    assert tm.cancel_token is not None
    assert not tm.cancel_token.is_cancelled
    # Pre-roll flushed into turn_audio.
    assert len(tm.turn_audio) > 0
    # Exactly one TurnStarted emitted for this turn.
    assert collector.type_names.count("TurnStarted") == 1


@pytest.mark.asyncio
async def test_manual_start_does_not_cancel_stale_token():
    """The manual path uses ``cancel_previous_token=False``.

    A stale token lingering while IDLE must be *replaced* by ``start_turn()``
    but NOT cancelled — only the barge-in path cancels the prior token.
    """
    bus = EventBus()
    tm = TurnManager(bus)

    stale = CancelToken()
    tm._cancel_token = stale

    await tm.start_turn()

    assert tm.state == TurnManagerState.USER_SPEAKING
    # Stale token replaced by a fresh one...
    assert tm.cancel_token is not stale
    # ...and NOT cancelled (manual path preserves the prior token).
    assert not stale.is_cancelled


@pytest.mark.asyncio
async def test_barge_in_during_processing_cancels_inflight_token():
    """VAD start during PROCESSING is a barge-in that cancels the in-flight turn.

    Drives IDLE -> USER_SPEAKING -> USER_PAUSED -> PROCESSING (via the silence
    timeout) then fires VADStartSpeaking.  Asserts a second TurnStarted is
    emitted, the prior cancel token is cancelled (so the in-flight agent run is
    stopped), and a fresh token is issued for the new turn.
    """
    bus = EventBus()
    config = TurnManagerConfig(end_of_turn_silence_ms=10)
    cancel_called = [False]

    async def mock_cancel():
        cancel_called[0] = True
        await bus.emit(Interruption())  # Real callback emits Interruption

    tm = TurnManager(bus, config=config, cancel_turn_callback=mock_cancel)
    collector = EventCollector(bus)

    # IDLE -> USER_SPEAKING -> USER_PAUSED -> PROCESSING via silence timeout
    await tm.on_vad_event(VADStartSpeaking())
    await tm.on_vad_event(VADStopSpeaking())
    await asyncio.sleep(0.05)
    assert tm.state == TurnManagerState.PROCESSING

    # Capture the in-flight turn's cancel token before the barge-in.
    inflight_token = tm.cancel_token
    assert inflight_token is not None
    assert not inflight_token.is_cancelled

    # User re-speaks while the agent is processing -> barge-in.
    await tm.on_vad_event(VADStartSpeaking())

    assert cancel_called[0]
    assert tm.state == TurnManagerState.USER_SPEAKING
    # Prior token cancelled so the stale agent response cannot leak through.
    assert inflight_token.is_cancelled
    # A fresh, non-cancelled token was issued for the new turn.
    assert tm.cancel_token is not None
    assert tm.cancel_token is not inflight_token
    assert not tm.cancel_token.is_cancelled
    # A new TurnStarted was emitted (original turn + barge-in turn).
    turn_started_count = sum(1 for n in collector.type_names if n == "TurnStarted")
    assert turn_started_count == 2


@pytest.mark.asyncio
async def test_barge_in_during_processing_logs_real_from_state(caplog):
    """The barge-in debug log must reflect the true from-state, not a hardcode.

    A barge-in out of PROCESSING used to log a hardcoded
    ``Turn: BotSpeaking -> UserSpeaking`` even though the journal recorded the
    real PROCESSING from-state.  The log line is now derived from the actual
    transition, so it must read ``processing -> user_speaking`` here.
    """
    bus = EventBus()
    config = TurnManagerConfig(end_of_turn_silence_ms=10)

    async def mock_cancel():
        await bus.emit(Interruption())

    tm = TurnManager(bus, config=config, cancel_turn_callback=mock_cancel)

    await tm.on_vad_event(VADStartSpeaking())
    await tm.on_vad_event(VADStopSpeaking())
    await asyncio.sleep(0.05)
    assert tm.state == TurnManagerState.PROCESSING

    # The ``easycat`` logger flips ``propagate = False`` once console logging is
    # enabled (see ``_logging.py``), which stops ``caplog``'s root handler from
    # seeing these records when an earlier test leaves that state in place. Attach
    # the capture handler directly to the emitting logger so this assertion is
    # robust to logging state leaked by other tests in the full suite.
    turn_logger = logging.getLogger("easycat.turn_manager")
    prev_level = turn_logger.level
    turn_logger.setLevel(logging.DEBUG)
    turn_logger.addHandler(caplog.handler)
    try:
        await tm.on_vad_event(VADStartSpeaking())
    finally:
        turn_logger.removeHandler(caplog.handler)
        turn_logger.setLevel(prev_level)

    assert tm.state == TurnManagerState.USER_SPEAKING
    barge_in_logs = [
        r.getMessage()
        for r in caplog.records
        if r.name == "easycat.turn_manager" and "barge_in" in r.getMessage()
    ]
    assert barge_in_logs, "expected a barge-in transition log line"
    # The from-state must be the real PROCESSING state, never a hardcoded one.
    assert "Turn: processing -> user_speaking (barge_in)" in barge_in_logs
    assert not any("bot_speaking -> user_speaking (barge_in)" in m for m in barge_in_logs)


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


@pytest.mark.asyncio
async def test_barge_in_via_push_to_talk_during_processing():
    """PTT start_turn during PROCESSING is a barge-in: cancel stale token, new turn."""
    bus = EventBus()
    config = TurnManagerConfig(mode=TurnMode.PUSH_TO_TALK)
    cancel_called = [False]

    async def mock_cancel():
        cancel_called[0] = True
        await bus.emit(Interruption())

    tm = TurnManager(bus, config=config, cancel_turn_callback=mock_cancel)
    collector = EventCollector(bus)

    # IDLE -> USER_SPEAKING -> PROCESSING via the PTT API.
    await tm.start_turn()
    await tm.end_turn()
    assert tm.state == TurnManagerState.PROCESSING

    inflight_token = tm.cancel_token
    assert inflight_token is not None
    assert not inflight_token.is_cancelled

    # PTT press again while agent is processing -> barge-in (was a silent no-op).
    await tm.start_turn()

    assert cancel_called[0]
    assert tm.state == TurnManagerState.USER_SPEAKING
    assert inflight_token.is_cancelled
    assert tm.cancel_token is not None
    assert tm.cancel_token is not inflight_token
    assert not tm.cancel_token.is_cancelled
    turn_started_count = sum(1 for n in collector.type_names if n == "TurnStarted")
    assert turn_started_count == 2


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

    # Capture the active token so we can assert reset() cancels it (matching
    # the barge-in teardown path) rather than abandoning it uncancelled.
    active_token = tm.cancel_token
    assert active_token is not None
    assert not active_token.is_cancelled

    tm.reset()

    assert tm.state == TurnManagerState.IDLE
    assert len(tm.turn_audio) == 0
    assert tm.cancel_token is None
    # The prior token must be cancelled so any in-flight work bound to it stops.
    assert active_token.is_cancelled


@pytest.mark.asyncio
async def test_reset_preserve_token_leaves_token_uncancelled():
    """reset(preserve_token=True) returns to IDLE but does NOT cancel the token.

    Contrasts ``test_reset_returns_to_idle`` (default ``reset()`` cancels the
    active token).  The gated keep-alive path needs the manager back at IDLE
    with buffers cleared while the current turn's token stays live.
    """
    bus = EventBus()
    tm = TurnManager(bus)

    await tm.on_vad_event(VADStartSpeaking())
    tm.on_audio_frame(_chunk())
    assert tm.state == TurnManagerState.USER_SPEAKING
    assert len(tm.turn_audio) > 0

    active_token = tm.cancel_token
    assert active_token is not None
    assert not active_token.is_cancelled

    tm.reset(preserve_token=True)

    assert tm.state == TurnManagerState.IDLE
    assert len(tm.turn_audio) == 0
    # Reference dropped either way, but the token itself is left uncancelled.
    assert tm.cancel_token is None
    assert not active_token.is_cancelled


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


@pytest.mark.parametrize(
    "field",
    [
        "end_of_turn_silence_ms",
        "punctuated_end_of_turn_silence_ms",
        "stt_segment_silence_ms",
        "pre_roll_ms",
    ],
)
def test_config_rejects_negative_values(field):
    """Negative timing values should fail at construction with a clear error."""
    with pytest.raises(ValueError, match=field):
        TurnManagerConfig(**{field: -1})


@pytest.mark.parametrize(
    "value",
    [-0.1, 1.1, float("nan"), float("inf"), True, "0.5"],
)
def test_config_rejects_invalid_endpoint_threshold(value: object) -> None:
    with pytest.raises(ValueError, match="endpoint_threshold"):
        TurnManagerConfig(endpoint_threshold=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0.0, 1.0])
def test_config_accepts_endpoint_threshold_boundaries(value: float) -> None:
    assert TurnManagerConfig(endpoint_threshold=value).endpoint_threshold == value
