"""Tests for ``AudioRouter`` extracted from Session in Phase 2."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat._bounded_queue import BoundedAudioQueue
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.cancel import CancelToken
from easycat.events import (
    AudioIn,
    AudioOut,
    Error,
    EventBus,
    PlaybackMarkAck,
    TransportAudioDelivered,
    TTSAudio,
)
from easycat.runtime.context import RunContext
from easycat.session._audio_router import AudioRouter
from easycat.session._journal_sink import SessionJournalSink
from easycat.session._turn_context import TurnContext
from easycat.stages.audio import AudioStage
from easycat.stages.stt import STTStage
from easycat.stages.transport import TransportStage
from easycat.stages.vad import VADStage
from easycat.turn_manager import TurnManager, TurnManagerConfig, TurnManagerState

# ── Test doubles ─────────────────────────────────────────────


class _FakeTransport:
    def __init__(self, chunks: list[AudioChunk] | None = None) -> None:
        self.chunks = chunks or []
        self.sent: list[AudioChunk] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        for chunk in self.chunks:
            yield chunk

    async def send_audio(self, chunk: AudioChunk) -> bool:
        self.sent.append(chunk)
        return True

    async def clear_audio(self) -> None:
        pass


class _AckTransport(_FakeTransport):
    """Transport that supports playback marks."""

    def __init__(self, chunks: list[AudioChunk] | None = None) -> None:
        super().__init__(chunks=chunks)
        self.marks: list[str] = []

    async def send_playback_mark(self, name: str | None = None) -> str:
        mark_name = name or f"mark_{len(self.marks) + 1}"
        self.marks.append(mark_name)
        return mark_name


class _PassthroughNR:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk

    def configure(self, **kwargs) -> None:
        pass


class _PassthroughAEC:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk

    def feed_reference(self, chunk: AudioChunk) -> None:
        pass

    def configure(self, **kwargs) -> None:
        pass


class _RecordingVAD:
    def __init__(self) -> None:
        self.calls: list[AudioChunk] = []

    async def process(self, chunk: AudioChunk) -> AsyncIterator:
        self.calls.append(chunk)
        if False:
            yield None

    def configure(self, **kwargs) -> None:
        pass


class _RecordingSTT:
    def __init__(self) -> None:
        self.received: list[AudioChunk] = []

    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.received.append(chunk)

    async def end_stream(self) -> None:
        pass

    async def events(self) -> AsyncIterator:
        if False:
            yield None


# ── Helpers ───────────────────────────────────────────────────


def _make_chunk(n_samples: int = 160, byte_value: int = 1) -> AudioChunk:
    return AudioChunk(data=bytes([byte_value]) * (n_samples * 2), format=PCM16_MONO_16K)


def _make_loud_chunk(n_samples: int = 160, amplitude: int = 6000) -> AudioChunk:
    sample = int(amplitude).to_bytes(2, "little", signed=True)
    return AudioChunk(data=sample * n_samples, format=PCM16_MONO_16K)


def _make_router(
    *,
    transport: _FakeTransport | None = None,
    is_stt_active: bool = False,
    auto_turn_from_stt_final: bool = False,
    enable_vad: bool = False,
    enable_aec: bool = False,
    enable_noise_reduction: bool = False,
    current_turn: TurnContext | None = None,
    outbound_queue: BoundedAudioQueue | None = None,
    turn_manager: TurnManager | None = None,
) -> tuple[AudioRouter, dict]:
    transport = transport or _FakeTransport()
    bus = EventBus()
    emitted: list = []

    async def _emit(event):
        emitted.append(event)
        await bus.emit(event)

    nr = _PassthroughNR()
    aec = _PassthroughAEC()
    vad = _RecordingVAD()
    stt = _RecordingSTT()

    audio_stage = AudioStage(nr, echo_canceller=aec if enable_aec else None)
    vad_stage = VADStage(vad)
    stt_stage = STTStage(stt)
    transport_stage = TransportStage(transport)

    tm = turn_manager or TurnManager(bus, config=TurnManagerConfig())
    no_turn = TurnContext(turn_id="no-turn", cancel_token=CancelToken())
    journal_sink = SessionJournalSink(
        event_bus=bus,
        journal=None,
        artifact_store=None,
        session_id="s",
        current_turn_id=lambda turn_id=None: turn_id,
    )
    run_ctx = RunContext(run_id="s", session_id="s", runtime_mode="chained_pipeline")
    queue = outbound_queue or BoundedAudioQueue(name="test_outbound")

    state: dict = {
        "running": True,
        "stt_active": is_stt_active,
        "current_turn": current_turn,
        "emitted": emitted,
        "transport": transport,
        "queue": queue,
        "stt": stt,
        "vad": vad,
        "audio_stage": audio_stage,
        "vad_stage": vad_stage,
        "stt_stage": stt_stage,
        "tm": tm,
        "bus": bus,
    }

    router = AudioRouter(
        transport=transport,
        audio_stage=audio_stage,
        vad_stage=vad_stage,
        stt_stage=stt_stage,
        transport_stage=transport_stage,
        turn_manager=tm,
        event_bus=bus,
        journal_sink=journal_sink,
        run_ctx=run_ctx,
        no_turn=no_turn,
        echo_canceller=aec,
        enable_noise_reduction=lambda: enable_noise_reduction,
        enable_aec=lambda: enable_aec,
        enable_vad=lambda: enable_vad,
        auto_turn_from_stt_final=lambda: auto_turn_from_stt_final,
        emit=_emit,
        is_running=lambda: state["running"],
        set_running=lambda v: state.update(running=v),
        current_turn=lambda: state["current_turn"],
        is_stt_active=lambda: state["stt_active"],
        outbound_queue=queue,
    )
    return router, state


# ── Tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingress_emits_audio_in_for_each_chunk():
    chunks = [_make_chunk(byte_value=i + 1) for i in range(3)]
    transport = _FakeTransport(chunks=chunks)
    router, state = _make_router(transport=transport)

    await router._run_pipeline()

    audio_ins = [evt for evt in state["emitted"] if isinstance(evt, AudioIn)]
    assert len(audio_ins) == 3


@pytest.mark.asyncio
async def test_ingress_skips_stt_when_inactive():
    chunks = [_make_chunk() for _ in range(2)]
    transport = _FakeTransport(chunks=chunks)
    router, state = _make_router(transport=transport, is_stt_active=False)

    await router._run_pipeline()

    assert len(state["stt"].received) == 0


@pytest.mark.asyncio
async def test_ingress_feeds_stt_when_active():
    chunks = [_make_chunk() for _ in range(2)]
    transport = _FakeTransport(chunks=chunks)
    turn = TurnContext(turn_id="t1", cancel_token=CancelToken())
    router, state = _make_router(
        transport=transport,
        is_stt_active=True,
        current_turn=turn,
    )

    await router._run_pipeline()

    assert len(state["stt"].received) == 2
    assert turn.stt_has_uncommitted_audio is True


@pytest.mark.asyncio
async def test_auto_turn_from_speech_energy_starts_after_two_loud_chunks():
    chunks = [_make_loud_chunk(), _make_loud_chunk()]
    transport = _FakeTransport(chunks=chunks)
    router, state = _make_router(
        transport=transport,
        auto_turn_from_stt_final=True,
        is_stt_active=False,
    )
    started: list[bool] = []

    async def _on_start_turn(*args, **kwargs):
        started.append(True)

    state["tm"].start_turn = _on_start_turn

    await router._run_pipeline()

    assert started == [True]
    assert router._auto_turn_speech_frames == 0


@pytest.mark.asyncio
async def test_reset_speech_detection_zeroes_counter():
    router, _ = _make_router()
    router._auto_turn_speech_frames = 5
    router.reset_speech_detection()
    assert router._auto_turn_speech_frames == 0


@pytest.mark.asyncio
async def test_outbound_drain_sends_queued_chunks_to_transport():
    transport = _FakeTransport()
    router, state = _make_router(transport=transport)
    state["running"] = False  # exit drain after queue empty

    await router.queue_outbound(_make_chunk(byte_value=7))
    await router.queue_outbound(_make_chunk(byte_value=8))

    await router._drain_outbound_audio()

    assert len(transport.sent) == 2


@pytest.mark.asyncio
async def test_playback_mark_emitted_after_byte_interval():
    transport = _AckTransport()
    turn = TurnContext(turn_id="t", cancel_token=CancelToken())
    router, state = _make_router(transport=transport, current_turn=turn)
    state["running"] = False
    # Lower the interval so a single chunk crosses it.
    router._playback_mark_bytes_interval = 100

    big_chunk = _make_chunk(n_samples=200)  # 400 bytes
    turn.bytes_since_last_mark = 0

    # Drain manually exercises the delivery path.
    await router.queue_outbound(big_chunk)
    await router._drain_outbound_audio()

    # bytes_since_last_mark grows in record_audio_sent inside the turn
    # context; here we check the router emitted at least one mark.
    assert len(transport.marks) >= 1


@pytest.mark.asyncio
async def test_on_playback_ack_records_byte_position():
    turn = TurnContext(turn_id="t", cancel_token=CancelToken())
    turn.playback_mark_to_bytes["m1"] = 1234
    router, _ = _make_router(current_turn=turn)

    router.on_playback_ack(PlaybackMarkAck(mark_name="m1"))

    assert "m1" not in turn.playback_mark_to_bytes
    assert turn.playback_ack_log
    assert turn.playback_ack_log[-1][1] == 1234


@pytest.mark.asyncio
async def test_on_playback_ack_unknown_mark_is_noop():
    turn = TurnContext(turn_id="t", cancel_token=CancelToken())
    router, _ = _make_router(current_turn=turn)

    router.on_playback_ack(PlaybackMarkAck(mark_name="unknown"))

    assert not turn.playback_ack_log


@pytest.mark.asyncio
async def test_gated_replay_enqueues_chunks_and_transitions_to_bot_speaking():
    router, state = _make_router()
    chunk1 = _make_chunk(byte_value=1)
    chunk2 = _make_chunk(byte_value=2)
    events = [TTSAudio(chunk=chunk1), TTSAudio(chunk=chunk2)]

    await router.gated_replay(events)

    assert state["tm"].state == TurnManagerState.BOT_SPEAKING
    assert state["queue"].qsize() == 2
    assert router._replay_chunks_pending == 2


@pytest.mark.asyncio
async def test_on_audio_delivered_emits_audio_out():
    turn = TurnContext(turn_id="t", cancel_token=CancelToken())
    router, state = _make_router(current_turn=turn)
    chunk = _make_chunk()

    await router.on_audio_delivered(
        TransportAudioDelivered(chunk=chunk, turn_ref=turn),
    )

    audio_outs = [evt for evt in state["emitted"] if isinstance(evt, AudioOut)]
    assert len(audio_outs) == 1
    assert audio_outs[0].turn_id == "t"


@pytest.mark.asyncio
async def test_start_and_stop_ingress_cancels_task():
    # Transport that never yields so the loop blocks until cancelled.
    class _StalledTransport(_FakeTransport):
        async def receive_audio(self) -> AsyncIterator[AudioChunk]:
            await asyncio.sleep(10)
            if False:
                yield None

    router, _ = _make_router(transport=_StalledTransport())
    task = router.start_ingress()
    assert router.pipeline_task is task
    await asyncio.sleep(0)  # yield to allow the task to start
    await router.stop_ingress()
    assert router.pipeline_task is None
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_await_drain_returns_immediately_when_queue_empty():
    router, _ = _make_router()
    # No outbound task started; must return immediately.
    await router.await_drain(timeout=0.1)


@pytest.mark.asyncio
async def test_stop_outbound_when_no_task_is_noop():
    router, _ = _make_router()
    await router.stop_outbound()
    assert router.outbound_task is None


@pytest.mark.asyncio
async def test_per_chunk_error_is_skipped_and_pipeline_survives():
    chunks = [_make_chunk(byte_value=i + 1) for i in range(3)]
    transport = _FakeTransport(chunks=chunks)
    turn = TurnContext(turn_id="t1", cancel_token=CancelToken())
    router, state = _make_router(
        transport=transport,
        is_stt_active=True,
        current_turn=turn,
    )

    # Fail STT execute on the second chunk only.
    calls = {"n": 0}
    original_send = state["stt"].send_audio

    async def _flaky_send(chunk):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("transient STT glitch")
        await original_send(chunk)

    state["stt"].send_audio = _flaky_send

    await router._run_pipeline()

    # One bad frame surfaced as an Error, but the other two were delivered
    # and the loop ran to completion (transport exhausted, not torn down).
    errors = [evt for evt in state["emitted"] if isinstance(evt, Error)]
    assert len(errors) == 1
    assert len(state["stt"].received) == 2
    assert state["running"] is False  # finally marks stopped on natural exit


@pytest.mark.asyncio
async def test_sustained_chunk_errors_tear_down_session():
    threshold = AudioRouter._MAX_CONSECUTIVE_CHUNK_ERRORS
    chunks = [_make_chunk() for _ in range(threshold + 5)]
    transport = _FakeTransport(chunks=chunks)
    turn = TurnContext(turn_id="t1", cancel_token=CancelToken())
    router, state = _make_router(
        transport=transport,
        is_stt_active=True,
        current_turn=turn,
    )

    async def _always_fail(chunk):
        raise RuntimeError("backend down")

    state["stt"].send_audio = _always_fail

    await router._run_pipeline()

    errors = [evt for evt in state["emitted"] if isinstance(evt, Error)]
    # One Error per failed frame up to the threshold; the threshold frame
    # re-raises into the fatal handler which emits one more Error.
    assert len(errors) == threshold + 1


@pytest.mark.asyncio
async def test_await_drain_waits_for_in_flight_send():
    release = asyncio.Event()

    class _SlowTransport(_FakeTransport):
        async def send_audio(self, chunk: AudioChunk) -> bool:
            await release.wait()
            self.sent.append(chunk)
            return True

    transport = _SlowTransport()
    router, state = _make_router(transport=transport)

    await router.queue_outbound(_make_chunk(byte_value=9))
    router.start_outbound()
    # Let the drain task dequeue the chunk and block inside send_audio.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Queue is empty but the chunk is still in flight: await_drain must
    # not return until the send completes (it will time out here).
    await router.await_drain(timeout=0.05)
    assert len(transport.sent) == 0  # still in flight, send_audio blocked

    # Releasing the send lets the in-flight chunk land and drain to idle.
    release.set()
    await router.await_drain(timeout=1.0)
    assert len(transport.sent) == 1

    state["running"] = False
    await router.stop_outbound()
