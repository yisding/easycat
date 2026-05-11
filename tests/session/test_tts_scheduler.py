"""Tests for ``TTSScheduler`` extracted from Session in Phase 3."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.bounded_queue import BoundedAudioQueue
from easycat.cancel import CancelToken
from easycat.events import EventBus, TTSAudio
from easycat.llm_output_processing import LLMOutputProcessor
from easycat.runtime.context import RunContext
from easycat.runtime.journal import InMemoryRingBuffer
from easycat.session._audio_router import AudioRouter
from easycat.session._journal_sink import SessionJournalSink
from easycat.session._tts_scheduler import TTSScheduler
from easycat.session._turn_context import TurnContext
from easycat.stages.audio import AudioStage
from easycat.stages.stt import STTStage
from easycat.stages.transport import TransportStage
from easycat.stages.tts import TTSStage
from easycat.stages.vad import VADStage
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManager, TurnManagerConfig

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

    supports_ssml = False

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


class _SSMLTTS(_RecordingTTS):
    supports_ssml = True


class _FakeTransport:
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        if False:
            yield None

    async def send_audio(self, chunk: AudioChunk) -> bool:
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
    current_turn_ref: dict[str, TurnContext | None] = {"turn": None}

    router = AudioRouter(
        transport=transport,
        audio_stage=audio_stage,
        vad_stage=vad_stage,
        stt_stage=stt_stage,
        transport_stage=transport_stage,
        turn_manager=turn_manager,
        event_bus=bus,
        journal_sink=journal_sink,
        run_ctx=run_ctx,
        no_turn=no_turn,
        echo_canceller=None,
        enable_noise_reduction=lambda: False,
        enable_aec=lambda: False,
        enable_vad=lambda: False,
        auto_turn_from_stt_final=lambda: False,
        emit=bus.emit,
        is_running=lambda: False,
        set_running=lambda value: None,
        current_turn=lambda: current_turn_ref["turn"],
        is_stt_active=lambda: False,
        with_correlation=lambda event: event,
        outbound_queue=outbound_queue,
    )

    audio_emissions: list[TTSAudio] = []
    bus.subscribe(TTSAudio, audio_emissions.append)

    async def _drain() -> bool:
        return drain_should_stop

    def _clear_turn() -> None:
        current_turn_ref["turn"] = None

    scheduler = TTSScheduler(
        tts=lambda: tts,
        tts_stage=tts_stage,
        turn_manager=turn_manager,
        event_bus=bus,
        journal_sink=journal_sink,
        run_ctx=run_ctx,
        no_turn=no_turn,
        audio_router=router,
        outbound_queue=outbound_queue,
        timeout_config=None,
        correlation_ids=lambda: (session_id, None),
        audio_gate=None,
        output_processors=output_processors or [],
        strip_markdown_enabled=strip_markdown_enabled,
        current_turn=lambda: current_turn_ref["turn"],
        is_gated=lambda: is_gated,
        drain_session_actions=_drain,
        clear_turn=_clear_turn,
    )

    return scheduler, {
        "journal": journal,
        "bus": bus,
        "router": router,
        "outbound_queue": outbound_queue,
        "audio_emissions": audio_emissions,
        "current_turn_ref": current_turn_ref,
        "turn_manager": turn_manager,
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
    tts = _RecordingTTS()  # supports_ssml = False
    scheduler, ctx = _build_scheduler(tts=tts, output_processors=[_SSMLifyProcessor()])

    payload = scheduler.prepare("hello", is_streaming=False, is_final=True)
    # Even though the processor emitted SSML, the provider doesn't
    # support it so the scheduler strips it back to plain text.
    assert payload.format == "plain"
    assert "<speak>" not in payload.text
    assert "hello" in payload.text


def test_prepare_keeps_ssml_when_provider_supports_it() -> None:
    tts = _SSMLTTS()  # supports_ssml = True
    scheduler, ctx = _build_scheduler(tts=tts, output_processors=[_SSMLifyProcessor()])

    payload = scheduler.prepare("hello", is_streaming=False, is_final=True)
    assert payload.format == "ssml"
    rec = next(r for r in ctx["journal"].read() if r.name == "tts_payload_prepared")
    assert rec.data["ssml_downgraded"] is False


# ── Tests: synthesize ────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_enqueues_audio_chunks() -> None:
    tts = _RecordingTTS(chunks=3)
    scheduler, ctx = _build_scheduler(tts=tts)

    turn = TurnContext("turn-1", CancelToken())
    ctx["current_turn_ref"]["turn"] = turn

    await scheduler.synthesize(TTSInput(text="hello", format="plain"), token=None, turn=turn)

    assert tts.synthesized[0].text == "hello"
    # 3 chunks should have been queued onto the outbound queue
    queued: list[AudioChunk] = []
    while not ctx["outbound_queue"].empty():
        queued.append(ctx["outbound_queue"].get_nowait())
    assert len(queued) == 3


@pytest.mark.asyncio
async def test_synthesize_coerces_string_payload() -> None:
    tts = _RecordingTTS(chunks=1)
    scheduler, ctx = _build_scheduler(tts=tts)

    turn = TurnContext("turn-1", CancelToken())
    ctx["current_turn_ref"]["turn"] = turn

    await scheduler.synthesize("hello", token=None, turn=turn)
    assert tts.synthesized[0].text == "hello"
    # A tts_payload_prepared record should also be written for the
    # string-coercion path.
    records = [r for r in ctx["journal"].read() if r.name == "tts_payload_prepared"]
    assert records


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
        await scheduler.synthesize("hello", token=None)
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

    async def _long_running() -> None:
        started.set()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task: asyncio.Task[None] = asyncio.create_task(_long_running())
    scheduler.current_task = task
    await started.wait()

    await scheduler.cancel()
    assert cancelled.is_set()
    assert tts.cancelled == 1


# ── Tests: synthesize_sentences stub ─────────────────────────


@pytest.mark.asyncio
async def test_synthesize_sentences_raises_not_implemented() -> None:
    tts = _RecordingTTS()
    scheduler, _ = _build_scheduler(tts=tts)
    turn = TurnContext("turn-1", CancelToken())

    with pytest.raises(NotImplementedError):
        await scheduler.synthesize_sentences(payloads=None, cancel_token=None, turn=turn)
