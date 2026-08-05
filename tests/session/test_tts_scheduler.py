"""Tests for ``TTSScheduler`` extracted from Session in Phase 3."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from easycat._bounded_queue import BoundedAudioQueue
from easycat._concurrency import RuntimeSupervisor
from easycat._epoch import Epoch
from easycat._turn_context import TurnContext
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.cancel import CancelToken
from easycat.events import BotStartedSpeaking, EventBus, TTSAudio
from easycat.llm_output_processing import LLMOutputProcessor
from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.context import RunContext
from easycat.runtime.scope import RuntimeScope
from easycat.session._audio_router import AudioRouter
from easycat.session._journal_sink import SessionJournalSink
from easycat.session._tts_scheduler import TTSScheduler
from easycat.session.actions import SessionActions
from easycat.stages.audio import AudioStage
from easycat.stages.stt import STTStage
from easycat.stages.transport import TransportStage
from easycat.stages.tts import TTSStage
from easycat.stages.vad import VADStage
from easycat.tts.input import TTSInput, TTSInputPolicy
from easycat.turn_manager import TurnManager, TurnManagerConfig, TurnManagerState
from tests.session._wiring_helpers import make_wiring

# ── Test doubles ─────────────────────────────────────────────


def _chunk() -> AudioChunk:
    return AudioChunk(data=b"\x00" * 320, format=PCM16_MONO_16K)


class _FakeTTSEvent:
    def __init__(self, audio: AudioChunk | None = None) -> None:
        from easycat.events import TTSEventType

        self.type = TTSEventType.AUDIO if audio else TTSEventType.MARKERS
        self.audio = audio
        self.markers = None


class _RecordingTTS:
    """TTS provider that records synthesize calls and emits N audio chunks."""

    def __init__(self, *, chunks: int = 1) -> None:
        self.chunks = chunks
        self.synthesized: list[TTSInput] = []
        self.cancelled = 0

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[_FakeTTSEvent]:
        self.synthesized.append(payload)
        for _ in range(self.chunks):
            yield _FakeTTSEvent(audio=_chunk())

    async def cancel(self) -> None:
        self.cancelled += 1


class _CoordinatedTTS(_RecordingTTS):
    """Expose provider start/release/finalization for lifecycle overlap tests."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finalized = asyncio.Event()

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[_FakeTTSEvent]:
        self.synthesized.append(payload)
        self.started.set()
        try:
            await self.release.wait()
            yield _FakeTTSEvent(audio=_chunk())
        finally:
            self.finalized.set()


class _SSMLTTS(_RecordingTTS):
    input_policy = TTSInputPolicy.native_ssml()


class _FakeTransport:
    def __init__(self) -> None:
        self.sent: list[AudioChunk] = []

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        if False:
            yield None

    async def send_audio(self, chunk: AudioChunk) -> bool:
        self.sent.append(chunk)
        return True

    async def clear_audio(self) -> None: ...


class _PassthroughNR:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk

    def configure(self, **_: object) -> None: ...


class _NoopVAD:
    async def process(self, chunk: AudioChunk) -> AsyncIterator:
        if False:
            yield None

    def configure(self, **_: object) -> None: ...


class _NoopSTT:
    async def start_stream(self) -> None: ...
    async def send_audio(self, chunk: AudioChunk) -> None: ...
    async def end_stream(self) -> None: ...

    async def events(self) -> AsyncIterator:
        if False:
            yield None


class _PrefixProcessor(LLMOutputProcessor):
    def __init__(self, prefix: str = "P:") -> None:
        self.prefix = prefix

    def process(self, payload: TTSInput, *, is_final: bool, is_streaming: bool) -> TTSInput:
        return TTSInput(text=f"{self.prefix}{payload.text}", format=payload.format)


class _SSMLifyProcessor(LLMOutputProcessor):
    """Processor that produces SSML output to test downgrade."""

    def process(self, payload: TTSInput, *, is_final: bool, is_streaming: bool) -> TTSInput:
        return TTSInput(text=f"<speak>{payload.text}</speak>", format="ssml")


# ── Fixtures / harness ───────────────────────────────────────


def _build_scheduler(
    *,
    tts: _RecordingTTS,
    output_processors: list[LLMOutputProcessor] | None = None,
    strip_markdown_enabled: bool = False,
    is_gated: bool = False,
    drain_should_stop: bool = False,
    is_running: bool = False,
    session_actions: SessionActions | None = None,
    drain_session_actions: Callable[[], Awaitable[bool]] | None = None,
) -> tuple[TTSScheduler, dict[str, object]]:
    bus = EventBus()
    journal = InMemoryRingBuffer()
    session_id = "session-test"
    journal_sink = SessionJournalSink(
        event_bus=bus,
        journal=journal,
        artifact_store=None,
        session_id=session_id,
        current_turn_id=lambda turn_id=None: turn_id,
    )
    run_ctx = RunContext(
        run_id=session_id,
        session_id=session_id,
        runtime_mode="chained_pipeline",
        journal=journal,
        artifact_store=None,
    )
    no_turn = TurnContext("no-turn", CancelToken())

    transport = _FakeTransport()
    turn_manager = TurnManager(bus, config=TurnManagerConfig())
    turn_manager.bind_session(session_id)

    audio_stage = AudioStage(_PassthroughNR(), echo_canceller=None, journal=journal)
    vad_stage = VADStage(_NoopVAD(), journal=journal)
    stt_stage = STTStage(_NoopSTT(), journal=journal)
    transport_stage = TransportStage(transport, journal=journal)
    tts_stage = TTSStage(tts, journal=journal)

    outbound_queue = BoundedAudioQueue(max_size=200, name="outbound")
    turn_identity: Epoch[TurnContext | None] = Epoch(None)
    running_ref = {"value": is_running}

    audio_emissions: list[TTSAudio] = []
    bus.subscribe(TTSAudio, audio_emissions.append)

    async def _drain() -> bool:
        return drain_should_stop

    def _clear_turn() -> None:
        turn_identity.bump(None)

    def _set_current_turn(turn: TurnContext | None) -> None:
        turn_identity.bump(turn)

    wiring = make_wiring(
        tts=lambda: tts,
        emit=bus.emit,
        is_running=lambda: running_ref["value"],
        current_turn=lambda: turn_identity.capture().value,
        capture_identity=turn_identity.capture,
        correlation_ids=lambda: (session_id, None),
        is_gated=lambda: is_gated,
        session_actions=lambda: session_actions,
        drain_session_actions=drain_session_actions or _drain,
        clear_turn=_clear_turn,
    )

    runtime_scope = RuntimeScope.create_root(
        name="session",
        root_id="test-tts-scheduler-session",
        supervisor=RuntimeSupervisor(capacity=1),
        survivor_capacity=1,
    )
    router = AudioRouter(
        wiring=wiring,
        transport=transport,
        audio_stage=audio_stage,
        vad_stage=vad_stage,
        stt_stage=stt_stage,
        transport_stage=transport_stage,
        turn_manager=turn_manager,
        event_bus=bus,
        journal_sink=journal_sink,
        runtime_scope=runtime_scope,
        run_ctx=run_ctx,
        no_turn=no_turn,
        echo_canceller=None,
        outbound_queue=outbound_queue,
    )

    scheduler = TTSScheduler(
        wiring=wiring,
        tts_stage=tts_stage,
        turn_manager=turn_manager,
        event_bus=bus,
        journal_sink=journal_sink,
        run_ctx=run_ctx,
        no_turn=no_turn,
        audio_router=router,
        outbound_queue=outbound_queue,
        timeout_config=None,
        audio_gate=None,
        output_processors=output_processors or [],
        strip_markdown_enabled=strip_markdown_enabled,
    )

    return scheduler, {
        "journal": journal,
        "bus": bus,
        "router": router,
        "outbound_queue": outbound_queue,
        "audio_emissions": audio_emissions,
        "current_turn": lambda: turn_identity.capture().value,
        "set_current_turn": _set_current_turn,
        "running_ref": running_ref,
        "turn_manager": turn_manager,
        "transport": transport,
    }


# ── Tests: prepare ───────────────────────────────────────────


def test_prepare_applies_output_processors_in_order() -> None:
    tts = _RecordingTTS()
    scheduler, _ = _build_scheduler(
        tts=tts,
        output_processors=[_PrefixProcessor("A:"), _PrefixProcessor("B:")],
    )

    payload = scheduler.prepare("hello", is_streaming=False, is_final=True)
    # B: applied last, so it wraps A:
    assert payload.text == "B:A:hello"
    assert payload.format == "plain"


def test_prepare_writes_tts_payload_prepared_journal_record() -> None:
    tts = _RecordingTTS()
    scheduler, ctx = _build_scheduler(tts=tts, output_processors=[_PrefixProcessor("X:")])

    scheduler.prepare("hi", is_streaming=True, is_final=False)
    records = [r for r in ctx["journal"].read() if r.name == "tts_payload_prepared"]
    assert len(records) == 1
    data = records[0].data
    assert data["original_text"] == "hi"
    assert data["prepared_text"] == "X:hi"
    assert data["is_streaming"] is True
    assert data["is_final"] is False
    assert data["processors"] == ["_PrefixProcessor"]
    assert data["ssml_downgraded"] is False


def test_prepare_strips_ssml_when_provider_does_not_support_it() -> None:
    tts = _RecordingTTS()
    scheduler, ctx = _build_scheduler(tts=tts, output_processors=[_SSMLifyProcessor()])

    payload = scheduler.prepare("hello", is_streaming=False, is_final=True)
    # Even though the processor emitted SSML, the provider doesn't
    # support it so the scheduler strips it back to plain text.
    assert payload.format == "plain"
    assert "<speak>" not in payload.text
    assert "hello" in payload.text
    rec = next(r for r in ctx["journal"].read() if r.name == "tts_payload_prepared")
    assert rec.data["ssml_downgraded"] is True


def test_prepare_keeps_ssml_when_provider_supports_it() -> None:
    tts = _SSMLTTS()
    scheduler, ctx = _build_scheduler(tts=tts, output_processors=[_SSMLifyProcessor()])

    payload = scheduler.prepare("hello", is_streaming=False, is_final=True)
    assert payload.format == "ssml"
    rec = next(r for r in ctx["journal"].read() if r.name == "tts_payload_prepared")
    assert rec.data["ssml_downgraded"] is False


# ── Tests: synthesize ────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_bypass_emits_chunks() -> None:
    tts = _RecordingTTS(chunks=2)
    scheduler, ctx = _build_scheduler(tts=tts)

    await scheduler.synthesize_bypass("greeting")
    assert tts.synthesized[0].text == "greeting"
    # Bypass synthesizes audio events on the bus regardless of gate.
    assert len(ctx["audio_emissions"]) == 2
    for emission in ctx["audio_emissions"]:
        assert emission.bypass_gate is True


@pytest.mark.asyncio
async def test_synthesize_sends_only_first_uncontended_chunk_inline() -> None:
    tts = _RecordingTTS(chunks=2)
    scheduler, ctx = _build_scheduler(tts=tts, is_running=True)
    router = ctx["router"]
    assert isinstance(router, AudioRouter)
    hold_active = asyncio.Event()
    active_task = asyncio.create_task(hold_active.wait())
    router._outbound_task = active_task

    try:
        await scheduler.synthesize_bypass("greeting")

        transport = ctx["transport"]
        assert isinstance(transport, _FakeTransport)
        assert len(transport.sent) == 1
        assert ctx["outbound_queue"].qsize() == 1
        ctx["running_ref"]["value"] = False
        await router._drain_outbound_audio()
        assert len(transport.sent) == 2
    finally:
        active_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active_task
        router._outbound_task = None


@pytest.mark.asyncio
async def test_begin_synthesis_overlaps_provider_with_bot_start_handlers() -> None:
    tts = _CoordinatedTTS()
    scheduler, ctx = _build_scheduler(tts=tts)
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    order: list[str] = []
    activities = []

    async def _slow_bot_started(_event: BotStartedSpeaking) -> None:
        assert tts.started.is_set()
        order.append("bot_started")
        handler_started.set()
        await release_handler.wait()

    ctx["bus"].subscribe(BotStartedSpeaking, _slow_bot_started)
    ctx["bus"].subscribe(TTSAudio, lambda _event: order.append("tts_audio"))

    begin_task = asyncio.create_task(
        scheduler.begin_synthesis_with_bot_start(
            TTSInput("hello"),
            None,
            is_active=lambda: True,
            activity_started=activities.append,
        )
    )
    await asyncio.wait_for(tts.started.wait(), timeout=0.5)
    await asyncio.wait_for(handler_started.wait(), timeout=0.5)

    tts.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert ctx["audio_emissions"] == []
    assert not begin_task.done()

    release_handler.set()
    synthesis_task = await begin_task
    result = await synthesis_task

    assert result.audio_produced is True
    assert order == ["bot_started", "tts_audio"]
    assert len(activities) == 1
    assert activities[0].is_current()
    assert activities[0].value is TurnManagerState.BOT_SPEAKING


@pytest.mark.asyncio
async def test_begin_synthesis_prefetches_while_waiting_for_agent_delta() -> None:
    tts = _CoordinatedTTS()
    scheduler, ctx = _build_scheduler(tts=tts)
    lifecycle_ready: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    bot_started: list[BotStartedSpeaking] = []
    ctx["bus"].subscribe(BotStartedSpeaking, bot_started.append)

    begin_task = asyncio.create_task(
        scheduler.begin_synthesis_with_bot_start(
            TTSInput("hello"),
            None,
            is_active=lambda: True,
            lifecycle_ready=lifecycle_ready,
        )
    )
    await asyncio.wait_for(tts.started.wait(), timeout=0.5)
    tts.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert bot_started == []
    assert ctx["audio_emissions"] == []
    assert not begin_task.done()

    lifecycle_ready.set_result(True)
    synthesis_task = await begin_task
    result = await synthesis_task

    assert result.audio_produced is True
    assert len(bot_started) == 1
    assert len(ctx["audio_emissions"]) == 1


@pytest.mark.asyncio
async def test_begin_synthesis_rejects_failed_agent_delta_dispatch() -> None:
    tts = _CoordinatedTTS()
    scheduler, ctx = _build_scheduler(tts=tts)
    lifecycle_ready: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    bot_started: list[BotStartedSpeaking] = []
    ctx["bus"].subscribe(BotStartedSpeaking, bot_started.append)

    begin_task = asyncio.create_task(
        scheduler.begin_synthesis_with_bot_start(
            TTSInput("hello"),
            None,
            is_active=lambda: True,
            lifecycle_ready=lifecycle_ready,
        )
    )
    await asyncio.wait_for(tts.started.wait(), timeout=0.5)
    tts.release.set()
    lifecycle_ready.set_result(False)

    synthesis_task = await begin_task

    assert synthesis_task.cancelled()
    assert tts.finalized.is_set()
    assert bot_started == []
    assert ctx["audio_emissions"] == []


@pytest.mark.asyncio
async def test_begin_synthesis_cancels_provider_when_lifecycle_dispatch_is_cancelled() -> None:
    tts = _CoordinatedTTS()
    scheduler, ctx = _build_scheduler(tts=tts)
    handler_started = asyncio.Event()
    never_release = asyncio.Event()

    async def _blocked_bot_started(_event: BotStartedSpeaking) -> None:
        handler_started.set()
        await never_release.wait()

    ctx["bus"].subscribe(BotStartedSpeaking, _blocked_bot_started)
    begin_task = asyncio.create_task(
        scheduler.begin_synthesis_with_bot_start(
            TTSInput("hello"),
            None,
            is_active=lambda: True,
        )
    )
    await asyncio.wait_for(tts.started.wait(), timeout=0.5)
    await asyncio.wait_for(handler_started.wait(), timeout=0.5)

    begin_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await begin_task

    assert tts.finalized.is_set()
    assert ctx["audio_emissions"] == []


@pytest.mark.asyncio
async def test_begin_synthesis_cleans_up_when_cancelled_during_initial_yield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat.session._tts_scheduler as scheduler_module

    tts = _CoordinatedTTS()
    scheduler, ctx = _build_scheduler(tts=tts)
    created_tasks: list[asyncio.Task[object]] = []
    real_create_task = asyncio.create_task

    def _capture_task(coro: Awaitable[object]) -> asyncio.Task[object]:
        task = real_create_task(coro)
        created_tasks.append(task)
        return task

    async def _cancel_initial_yield(_delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(scheduler_module.asyncio, "create_task", _capture_task)
    monkeypatch.setattr(scheduler_module.asyncio, "sleep", _cancel_initial_yield)

    with pytest.raises(asyncio.CancelledError):
        await scheduler.begin_synthesis_with_bot_start(
            TTSInput("hello"),
            None,
            is_active=lambda: True,
        )

    assert len(created_tasks) == 1
    assert created_tasks[0].cancelled()
    assert ctx["audio_emissions"] == []


@pytest.mark.asyncio
async def test_begin_synthesis_cancels_provider_when_lifecycle_dispatch_fails() -> None:
    tts = _CoordinatedTTS()
    tts.release.set()
    scheduler, ctx = _build_scheduler(tts=tts)

    async def _fail_bot_started() -> None:
        raise RuntimeError("lifecycle failed")

    ctx["turn_manager"].bot_started_speaking = _fail_bot_started

    with pytest.raises(RuntimeError, match="lifecycle failed"):
        await scheduler.begin_synthesis_with_bot_start(
            TTSInput("hello"),
            None,
            is_active=lambda: True,
        )

    assert tts.finalized.is_set()
    assert ctx["audio_emissions"] == []


@pytest.mark.asyncio
async def test_synthesize_short_circuits_when_playback_suppressed() -> None:
    """``is_playback_suppressed=True`` short-circuits future synth calls.

    This mirrors the contract used by the streaming agent loop: the
    consumer checks ``scheduler.is_playback_suppressed`` before calling
    into the synthesizer to drop pending payloads.
    """
    tts = _RecordingTTS(chunks=1)
    scheduler, _ = _build_scheduler(tts=tts)

    scheduler.set_playback_suppressed(True)
    assert scheduler.is_playback_suppressed is True
    # The streaming consumer pattern: skip synth when suppressed.
    if not scheduler.is_playback_suppressed:
        await scheduler.synthesizer.synthesize("hello", token=None)
    assert tts.synthesized == []


# ── Tests: cancel ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_invokes_synth_cancel() -> None:
    tts = _RecordingTTS()
    scheduler, _ = _build_scheduler(tts=tts)

    await scheduler.cancel()
    assert tts.cancelled == 1


@pytest.mark.asyncio
async def test_cancel_cancels_pending_current_task() -> None:
    tts = _RecordingTTS()
    scheduler, _ = _build_scheduler(tts=tts)

    started = asyncio.Event()
    cancelled = asyncio.Event()
    never_released = asyncio.Event()

    async def _long_running() -> None:
        started.set()
        try:
            await never_released.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task: asyncio.Task[None] = asyncio.create_task(_long_running())
    scheduler.active_turn_task = task
    await started.wait()

    await scheduler.cancel()
    assert cancelled.is_set()
    assert tts.cancelled == 1


@pytest.mark.asyncio
async def test_detached_cancel_drains_only_captured_turn_task() -> None:
    tts = _RecordingTTS()
    scheduler, _ = _build_scheduler(tts=tts)
    old_started = asyncio.Event()
    old_cancelled = asyncio.Event()
    hold_old = asyncio.Event()
    hold_successor = asyncio.Event()

    async def _old_turn() -> None:
        old_started.set()
        try:
            await hold_old.wait()
        except asyncio.CancelledError:
            old_cancelled.set()
            raise

    old_task = asyncio.create_task(_old_turn())
    scheduler.active_turn_task = old_task
    await old_started.wait()

    captured = scheduler.request_turn_cancel()
    successor_task = asyncio.create_task(hold_successor.wait())
    scheduler.active_turn_task = successor_task
    await scheduler.finish_turn_cancel(captured)

    assert old_cancelled.is_set()
    assert not successor_task.done()
    assert tts.cancelled == 1

    successor_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await successor_task


# ── Tests: turn finalization ────────────────────────────────


@pytest.mark.asyncio
async def test_finalize_speaking_turn_keeps_no_interrupt_until_outbound_drain() -> None:
    actions = SessionActions()
    actions.end_call(reason="done")
    observations: list[tuple[str, bool]] = []

    async def _drain() -> bool:
        actions.drain(preserve_no_interrupt=True)
        observations.append(("after_action_drain", actions.no_interrupt))
        return True

    tts = _RecordingTTS()
    scheduler, ctx = _build_scheduler(
        tts=tts,
        session_actions=actions,
        drain_session_actions=_drain,
    )
    turn = TurnContext("turn-1", CancelToken())
    ctx["set_current_turn"](turn)
    turn_manager = ctx["turn_manager"]
    await turn_manager.bot_started_speaking()

    async def _await_drain() -> None:
        observations.append(("during_outbound_drain", actions.no_interrupt))

    ctx["router"].await_drain = _await_drain

    should_stop = await scheduler.finalize_speaking_turn(turn)

    assert should_stop is True
    assert observations == [
        ("after_action_drain", True),
        ("during_outbound_drain", True),
    ]
    assert actions.no_interrupt is False
    assert turn_manager.state.value == "idle"


@pytest.mark.asyncio
async def test_finalize_speaking_turn_does_not_clear_turn_started_during_mark_flush() -> None:
    tts = _RecordingTTS()
    scheduler, ctx = _build_scheduler(tts=tts)
    old_turn = TurnContext("old-turn", CancelToken())
    new_turn = TurnContext("new-turn", CancelToken())
    ctx["set_current_turn"](old_turn)
    turn_manager = ctx["turn_manager"]
    await turn_manager.bot_started_speaking()

    flush_started = asyncio.Event()
    release_flush = asyncio.Event()

    async def _flush_trailing_playback_mark(turn: TurnContext | None = None) -> None:
        assert turn is old_turn
        flush_started.set()
        await release_flush.wait()

    ctx["router"].flush_trailing_playback_mark = _flush_trailing_playback_mark

    finalize_task = asyncio.create_task(scheduler.finalize_speaking_turn(old_turn))
    await flush_started.wait()
    ctx["set_current_turn"](new_turn)

    release_flush.set()
    should_stop = await finalize_task

    assert should_stop is False
    assert ctx["current_turn"]() is new_turn
    assert turn_manager.state.value == "idle"


@pytest.mark.asyncio
async def test_finalize_speaking_turn_rejects_same_object_republication() -> None:
    """An epoch bump must stale the finalizer even when the pointer is unchanged."""
    drain_entered = asyncio.Event()
    release_drain = asyncio.Event()

    async def _drain() -> bool:
        drain_entered.set()
        await release_drain.wait()
        return False

    scheduler, ctx = _build_scheduler(tts=_RecordingTTS(), drain_session_actions=_drain)
    turn = TurnContext("reissued-turn", CancelToken())
    ctx["set_current_turn"](turn)
    turn_manager = ctx["turn_manager"]
    await turn_manager.bot_started_speaking()

    finalizer = asyncio.create_task(scheduler.finalize_speaking_turn(turn))
    await drain_entered.wait()
    ctx["set_current_turn"](turn)
    release_drain.set()

    assert await finalizer is False
    assert ctx["current_turn"]() is turn
    assert turn_manager.state.value == "bot_speaking"


@pytest.mark.asyncio
async def test_finalize_speaking_turn_rejects_same_state_activity_republication() -> None:
    """A BOT_SPEAKING re-publication must fence the prior finalizer lease."""
    drain_entered = asyncio.Event()
    release_drain = asyncio.Event()

    async def _drain() -> bool:
        drain_entered.set()
        await release_drain.wait()
        return False

    scheduler, ctx = _build_scheduler(tts=_RecordingTTS(), drain_session_actions=_drain)
    turn = TurnContext("activity-reissued", CancelToken())
    ctx["set_current_turn"](turn)
    turn_manager = ctx["turn_manager"]
    await turn_manager.bot_started_speaking()

    finalizer = asyncio.create_task(scheduler.finalize_speaking_turn(turn))
    await drain_entered.wait()
    turn_manager._state = TurnManagerState.BOT_SPEAKING
    release_drain.set()

    assert await finalizer is False
    assert ctx["current_turn"]() is turn
    assert turn_manager.state is TurnManagerState.BOT_SPEAKING


@pytest.mark.asyncio
async def test_finalize_speaking_turn_does_not_stop_successor_during_action_drain() -> None:
    """Cancellation-resistant action cleanup must not stop a barge-in successor."""
    drain_entered = asyncio.Event()
    release_drain = asyncio.Event()

    async def _drain() -> bool:
        drain_entered.set()
        try:
            await release_drain.wait()
        except asyncio.CancelledError:
            await release_drain.wait()
        return False

    tts = _RecordingTTS()
    scheduler, ctx = _build_scheduler(tts=tts, drain_session_actions=_drain)
    old_turn = TurnContext("old-turn", CancelToken())
    successor = TurnContext("successor", CancelToken())
    ctx["set_current_turn"](old_turn)
    turn_manager = ctx["turn_manager"]
    await turn_manager.bot_started_speaking()

    finalizer = asyncio.create_task(scheduler.finalize_speaking_turn(old_turn))
    await drain_entered.wait()
    finalizer.cancel()

    ctx["set_current_turn"](successor)
    turn_manager.reset()
    turn_manager.begin_application_turn(successor.id, successor.cancel_token)
    await turn_manager.bot_started_speaking()

    release_drain.set()
    assert await finalizer is False
    assert turn_manager.state.value == "bot_speaking"


# ── Tests: synthesize_sentences stub ─────────────────────────


@pytest.mark.asyncio
async def test_synthesize_sentences_raises_not_implemented() -> None:
    tts = _RecordingTTS()
    scheduler, _ = _build_scheduler(tts=tts)
    turn = TurnContext("turn-1", CancelToken())

    with pytest.raises(NotImplementedError):
        await scheduler._synthesize_sentences(payloads=None, cancel_token=None, turn=turn)
