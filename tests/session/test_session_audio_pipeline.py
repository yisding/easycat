"""Session audio pipeline, STT commit, and auto-turn tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from easycat._turn_context import TurnContext
from easycat.audio_format import AudioChunk
from easycat.cancel import CancelToken
from easycat.events import (
    AgentDelta,
    AgentFinal,
    AudioIn,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    Error,
    Event,
    STTFinal,
    ToolCallResult,
    ToolCallStarted,
    TTSAudio,
    TurnEnded,
    TurnStarted,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.noise_reduction import PassthroughNoiseReducer
from easycat.session._session import Session
from easycat.session._types import TurnState
from easycat.timeouts import AgentTimeoutError
from easycat.turn_manager import TurnManagerConfig, TurnManagerState
from tests.session._session_core_helpers import (
    _FAST_TURN,
    AutoTurnSTT,
    FakeAgent,
    FakeSTT,
    FakeTransport,
    FakeTTS,
    FakeVAD,
    SegmentingSTT,
    _full_config,
    _make_chunk,
    _make_loud_chunk,
)


def test_stt_segment_silence_ms_forwarded_to_committer():
    """``TurnManagerConfig.stt_segment_silence_ms`` is read by Session, not
    TurnManager, and forwarded to the STTCommitter as ``segment_silence_ms``.

    Guards the (intentional) cross-component wiring documented on the field so
    it cannot silently regress into a dead config value.
    """
    session = Session(
        _full_config(
            turn_manager_config=TurnManagerConfig(
                end_of_turn_silence_ms=1000,
                stt_segment_silence_ms=250,
            ),
        )
    )
    assert session._stt_committer._segment_silence_ms == 250


def test_enable_noise_reduction_with_passthrough_does_not_raise():
    """enable_noise_reduction=True + a passthrough reducer must not crash.

    Graceful degradation when no optional backend is installed mirrors
    PassthroughAEC: ``create_noise_reducer`` already logs an actionable
    warning, so the Session must warn-and-continue rather than reject the
    passthrough at construction (the former noop-guard contradiction).
    """
    session = Session(
        _full_config(
            noise_reducer=PassthroughNoiseReducer(),
            enable_noise_reduction=True,
        )
    )
    assert session is not None


@pytest.mark.asyncio
async def test_schedule_turn_ended_cancels_inflight_stt_commit():
    """Regression test for plan-7 flakiness.

    When VADStopSpeaking fires, ``STTCommitter.schedule`` creates
    a task that calls ``stt.commit_segment``.  If SmartTurn immediately
    declares the turn complete, ``TurnEnded`` fires before the commit
    task has a chance to cancel — and previously ``schedule_turn_ended``
    only cancelled the *scheduled* task, not the *in-flight* one.  That
    left ``commit_segment`` racing with ``handle_end_of_speech``'s
    ``end_stream`` which issues its own commit: the first commit
    cleared the STT server's buffer and the second commit failed with
    "buffer too small".
    """
    config = _full_config()
    session = Session(config)
    session._is_running = True
    session._turn_state = TurnState.LISTENING
    session._turn = TurnContext("race-turn", CancelToken())
    session._turn.stt_has_uncommitted_audio = True
    session._stt_committer.mark_active()
    session._stt_committer._segment_silence_ms = 0  # match plan-7's fast config
    session._turn_manager._state = TurnManagerState.USER_PAUSED

    events = []
    commit_started = asyncio.Event()

    class _RaceSTT:
        async def start_stream(self) -> None: ...
        async def send_audio(self, chunk) -> None: ...

        async def commit_segment(self) -> bool:
            events.append("commit")
            commit_started.set()
            await asyncio.Event().wait()
            events.append("commit_done")
            return True

        async def end_stream(self) -> None:
            events.append("end_stream")

        async def events(self):
            return
            yield

    session.stt = _RaceSTT()
    session._stt_stage = type(session._stt_stage)(session.stt, journal=session._journal)

    session._stt_committer.schedule(VADStopSpeaking(), turn=session._turn)
    await asyncio.wait_for(commit_started.wait(), timeout=1.0)
    commit_task = session._stt_committer._segment_commit_task
    assert commit_task is not None
    session._turn_runner.schedule_turn_ended(TurnEnded(turn_id="race-turn"))
    with pytest.raises(asyncio.CancelledError):
        await commit_task
    if session._tts_scheduler.active_turn_task is not None:
        await session._tts_scheduler.active_turn_task

    # Invariant: we never observe BOTH commit_done AND end_stream in
    # the same run — the in-flight cancel closes the window.
    assert not ("commit_done" in events and "end_stream" in events), (
        f"in-flight commit was not cancelled on TurnEnded: events={events}"
    )


@pytest.mark.asyncio
async def test_pipeline_emits_audio_in_events():
    chunks = [_make_chunk(), _make_chunk()]
    transport = FakeTransport(chunks=chunks)
    config = _full_config(transport=transport, enable_vad=False)
    session = Session(config)

    received: list[AudioIn] = []
    received_all = asyncio.Event()

    def _record_audio_in(event: AudioIn) -> None:
        received.append(event)
        if len(received) == len(chunks):
            received_all.set()

    session.event_bus.subscribe(AudioIn, _record_audio_in)
    await session.start()
    try:
        await asyncio.wait_for(received_all.wait(), timeout=1.0)
    finally:
        await session.stop()

    assert len(received) == 2


@pytest.mark.asyncio
async def test_flux_auto_turn_does_not_start_on_silence_frames():
    transport = FakeTransport(chunks=[_make_chunk(), _make_chunk(), _make_chunk()])
    session = Session(
        _full_config(transport=transport, enable_vad=False, auto_turn_from_stt_final=True)
    )
    session._is_running = True
    session._turn_manager.start_turn = AsyncMock()  # type: ignore[method-assign]

    await session._audio_router._run_pipeline()

    session._turn_manager.start_turn.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_flux_auto_turn_does_not_barge_in_during_bot_playback():
    transport = FakeTransport(chunks=[_make_loud_chunk(), _make_loud_chunk(), _make_loud_chunk()])
    session = Session(
        _full_config(transport=transport, enable_vad=False, auto_turn_from_stt_final=True)
    )
    session._is_running = True
    session._turn_manager._state = TurnManagerState.BOT_SPEAKING
    session._turn_manager.start_turn = AsyncMock()  # type: ignore[method-assign]

    await session._audio_router._run_pipeline()

    session._turn_manager.start_turn.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_flux_auto_turn_starts_once_and_ends_on_stt_final():
    chunks = [_make_loud_chunk(), _make_loud_chunk(), _make_loud_chunk()]
    transport = FakeTransport(chunks=chunks)
    stt = AutoTurnSTT()
    session = Session(
        _full_config(
            transport=transport,
            stt=stt,
            enable_vad=False,
            auto_turn_from_stt_final=True,
        )
    )

    events_received: list[Event] = []
    agent_finished = asyncio.Event()

    def _record_turn_event(event: Event) -> None:
        events_received.append(event)
        if isinstance(event, AgentFinal):
            agent_finished.set()

    for event_type in (TurnStarted, STTFinal, TurnEnded, AgentFinal):
        session.event_bus.subscribe(event_type, _record_turn_event)

    await session.start()
    try:
        await asyncio.wait_for(agent_finished.wait(), timeout=1.0)

        type_names = [type(event).__name__ for event in events_received]
        assert type_names.count("TurnStarted") == 1
        assert "STTFinal" in type_names
        assert "TurnEnded" in type_names
        assert "AgentFinal" in type_names
        assert stt.start_count == 1
        assert stt.end_count == 1
        assert stt.sent_chunks == chunks
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_pipeline_noise_reduction():
    chunk = _make_chunk()
    transport = FakeTransport(chunks=[chunk])

    class TrackingNoiseReducer:
        def __init__(self) -> None:
            self.processed = False
            self.processed_event = asyncio.Event()

        async def process(self, c: AudioChunk) -> AudioChunk:
            self.processed = True
            self.processed_event.set()
            return c

    nr = TrackingNoiseReducer()
    config = _full_config(
        transport=transport, noise_reducer=nr, enable_vad=False, enable_noise_reduction=True
    )
    session = Session(config)

    await session.start()
    try:
        await asyncio.wait_for(nr.processed_event.wait(), timeout=1.0)
    finally:
        await session.stop()

    assert nr.processed


@pytest.mark.asyncio
async def test_handle_end_of_speech_clears_turn_id_on_stt_timeout():
    session = Session(_full_config())
    session._turn = TurnContext("turn-stale", CancelToken())
    session._timeout_config.stt_timeout = 0.01
    session._stt_committer.mark_active()
    # A pending segment future that never resolves drives the
    # committer's own stt_timeout fallback (await_pending), which
    # clears the turn instead of running the agent.
    session._turn.pending_stt_segment_futures.append(asyncio.get_running_loop().create_future())

    await session._turn_runner.handle_end_of_speech()

    assert session._turn is None
    assert session.turn_state == TurnState.IDLE


@pytest.mark.asyncio
async def test_handle_end_of_speech_clears_turn_id_on_empty_transcript():
    session = Session(_full_config())
    session._turn = TurnContext("turn-stale", CancelToken())

    await session._turn_runner.handle_end_of_speech()

    assert session._turn is None
    assert session.turn_state == TurnState.IDLE


@pytest.mark.asyncio
async def test_pause_commit_keeps_turn_open_but_collects_segment_final():
    stt = SegmentingSTT(["hello"])
    session = Session(
        _full_config(
            stt=stt,
            turn_manager_config=TurnManagerConfig(
                end_of_turn_silence_ms=1000,
                stt_segment_silence_ms=1,
            ),
        )
    )
    session._turn = TurnContext("turn-1", CancelToken())
    session._turn.stt_has_uncommitted_audio = True
    session._stt_committer.mark_active()
    session._turn_manager._state = TurnManagerState.USER_PAUSED
    session._stt_committer.start_event_loop(session._turn)

    try:
        session._stt_committer.schedule(VADStopSpeaking(), turn=session._turn)
        pause_task = session._stt_committer._pause_commit_task
        assert pause_task is not None
        await pause_task
        await session._stt_committer.await_inflight_commit()
        await session._stt_committer.await_pending(session._turn)

        assert stt.commit_calls == 1
        assert session._turn is not None
        assert session._turn_manager.state == TurnManagerState.USER_PAUSED
        assert session._turn.transcript_text == "hello"
    finally:
        await session._stt_committer.cancel(session._turn)


@pytest.mark.asyncio
async def test_handle_end_of_speech_no_duplicate_stt_final():
    """handle_end_of_speech must not re-emit per-segment STTFinals."""
    session = Session(_full_config())
    session._turn = TurnContext("turn-stale", CancelToken())
    session._turn.append_stt_segment("hello")
    session._turn.append_stt_segment("world")

    timeline: list[Event] = []
    session.event_bus.subscribe(STTFinal, lambda e: timeline.append(e))
    session.event_bus.subscribe(AgentFinal, lambda e: timeline.append(e))

    await session._turn_runner.handle_end_of_speech()

    stt_finals = [e for e in timeline if isinstance(e, STTFinal)]
    assert len(stt_finals) == 0
    agent_finals = [e for e in timeline if isinstance(e, AgentFinal)]
    assert len(agent_finals) == 1
    assert agent_finals[0].text == "HELLO WORLD"


@pytest.mark.asyncio
async def test_streaming_agent_timeout_emits_error_and_leaves_state_idle():
    errors: list[Error] = []

    class TimeoutAgent:
        async def run(self, text: str) -> str:
            raise AgentTimeoutError(timeout=0.01)

    session = Session(_full_config(agent=TimeoutAgent()))
    session.event_bus.subscribe(Error, lambda e: errors.append(e))
    session._turn = TurnContext("turn-stale", CancelToken())

    await session._turn_runner.run_streaming_agent("call me at 415-555-2671", token=None)

    assert session.turn_state == TurnState.IDLE
    assert any(isinstance(e.exception, AgentTimeoutError) for e in errors)


@pytest.mark.asyncio
async def test_pipeline_full_turn_with_provider_events():
    """Full pipeline using provider-scoped events (STTEvent, TTSEvent)."""
    chunks = [_make_chunk(), _make_chunk()]
    transport = FakeTransport(chunks=chunks)
    vad = FakeVAD()
    stt = FakeSTT(transcript="hello")
    agent = FakeAgent()
    tts = FakeTTS()

    config = _full_config(
        transport=transport,
        vad=vad,
        stt=stt,
        agent=agent,
        tts=tts,
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    events_received: list[Event] = []
    bot_stopped = asyncio.Event()

    def _record_pipeline_event(event: Event) -> None:
        events_received.append(event)
        if isinstance(event, BotStoppedSpeaking):
            bot_stopped.set()

    for et in [
        AudioIn,
        VADStartSpeaking,
        VADStopSpeaking,
        TurnStarted,
        STTFinal,
        ToolCallResult,
        ToolCallStarted,
        AgentDelta,
        AgentFinal,
        BotStartedSpeaking,
        TTSAudio,
        BotStoppedSpeaking,
        TurnEnded,
    ]:
        session.event_bus.subscribe(et, _record_pipeline_event)

    await session.start()
    try:
        await asyncio.wait_for(bot_stopped.wait(), timeout=1.0)
    finally:
        await session.stop()

    type_names = [type(e).__name__ for e in events_received]
    assert "AudioIn" in type_names
    assert "VADStartSpeaking" in type_names
    assert "VADStopSpeaking" in type_names
    assert "TurnStarted" in type_names
    assert "TurnEnded" in type_names
    assert "STTFinal" in type_names
    assert "AgentFinal" in type_names
    assert "BotStartedSpeaking" in type_names
    assert "TTSAudio" in type_names
    assert "BotStoppedSpeaking" in type_names

    turn_end_idx = type_names.index("TurnEnded")
    bot_start_idx = type_names.index("BotStartedSpeaking")
    bot_stop_idx = type_names.index("BotStoppedSpeaking")
    assert turn_end_idx < bot_start_idx
    assert turn_end_idx < bot_stop_idx

    # Verify agent uppercased the transcript
    agent_finals = [e for e in events_received if isinstance(e, AgentFinal)]
    assert len(agent_finals) == 1
    assert agent_finals[0].text == "HELLO"

    # Verify transport received TTS audio
    assert len(transport.sent) > 0


@pytest.mark.asyncio
async def test_pipeline_skips_empty_transcript():
    chunks = [_make_chunk(), _make_chunk()]
    transport = FakeTransport(chunks=chunks)
    vad = FakeVAD()
    stt = FakeSTT(transcript="")

    agent_ran = False

    class TrackingAgent:
        async def run(self, text: str) -> str:
            nonlocal agent_ran
            agent_ran = True
            return text

    config = _full_config(
        transport=transport,
        vad=vad,
        stt=stt,
        agent=TrackingAgent(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.15)
    await session.stop()

    assert not agent_ran


@pytest.mark.asyncio
async def test_active_turn_gated_to_non_idle_state():
    """``_active_turn`` and the correlation/TTS scheduler share one IDLE gate.

    The live ``self._turn`` pointer is deliberately kept alive past IDLE for
    gated-TTS playback-mark bookkeeping, but it must not count as the active
    turn (no stale turn_id stamping, no stale TTS correlation) once the turn
    manager has returned to IDLE.
    """
    session = Session(_full_config())
    session._turn = TurnContext("turn-active", CancelToken())

    # The TTS scheduler stamps the same active-turn id onto synthesized audio,
    # so it must share the exact gate via the same ``_active_turn`` helper.
    tts_correlation = session._tts_scheduler._synth._correlation_ids

    # Active while the turn manager is mid-turn.
    session._turn_manager._state = TurnManagerState.BOT_SPEAKING
    assert session._active_turn() is session._turn
    stamped = session._with_correlation(AudioIn(chunk=_make_chunk()))
    assert stamped.turn_id == "turn-active"
    assert tts_correlation() == (session.session_id, "turn-active")

    # Once IDLE, the still-alive turn pointer must not be treated as active.
    session._turn_manager._state = TurnManagerState.IDLE
    assert session._active_turn() is None
    not_stamped = session._with_correlation(AudioIn(chunk=_make_chunk()))
    assert not_stamped.turn_id is None
    assert tts_correlation() == (session.session_id, None)


@pytest.mark.asyncio
async def test_turn_state_idle_after_basic_agent_turn():
    """After a normal basic-agent turn completes, the session should be IDLE.
    The turn context may still exist (only cleared on next turn start or reset),
    but the turn state should be IDLE."""
    chunks = [_make_chunk(), _make_chunk()]
    transport = FakeTransport(chunks=chunks)
    config = _full_config(
        transport=transport,
        vad=FakeVAD(),
        stt=FakeSTT(transcript="hi"),
        agent=FakeAgent(),
        tts=FakeTTS(),
        turn_manager_config=_FAST_TURN,
    )
    session = Session(config)
    bot_stopped = asyncio.Event()
    session.event_bus.subscribe(BotStoppedSpeaking, lambda _event: bot_stopped.set())

    await session.start()
    try:
        await asyncio.wait_for(bot_stopped.wait(), timeout=1.0)
    finally:
        await session.stop()

    assert session.turn_state == TurnState.IDLE
