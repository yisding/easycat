"""Tests for ``AudioRouter`` extracted from Session in Phase 2."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat._bounded_queue import BoundedAudioQueue
from easycat._concurrency import RuntimeSupervisor
from easycat._turn_context import TurnContext
from easycat.audio_format import PCM16_MONO_16K, PCM16_MONO_24K, AudioChunk
from easycat.cancel import CancelToken
from easycat.events import (
    AudioIn,
    AudioOut,
    Error,
    EventBus,
    PlaybackMarkAck,
    TransportAudioDelivered,
    TransportDegraded,
    TTSAudio,
)
from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.context import RunContext
from easycat.runtime.scope import RuntimeScope
from easycat.session._audio_router import AudioRouter
from easycat.session._journal_sink import SessionJournalSink
from easycat.stages.audio import AudioStage
from easycat.stages.stt import STTStage
from easycat.stages.transport import TransportStage
from easycat.stages.vad import VADStage
from easycat.turn_manager import TurnManager, TurnManagerConfig, TurnManagerState
from tests.session._wiring_helpers import make_wiring

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
    stt: object | None = None,
    journal: InMemoryRingBuffer | None = None,
) -> tuple[AudioRouter, dict]:
    transport = transport or _FakeTransport()
    bus = EventBus()
    emitted: list = []
    journal = journal if journal is not None else InMemoryRingBuffer(capacity=64)
    runtime_supervisor = RuntimeSupervisor(capacity=1)
    runtime_scope = RuntimeScope.create_root(
        name="session",
        root_id="test-audio-router-session",
        supervisor=runtime_supervisor,
        survivor_capacity=1,
    )

    async def _emit(event):
        emitted.append(event)
        await bus.emit(event)

    nr = _PassthroughNR()
    aec = _PassthroughAEC()
    vad = _RecordingVAD()
    stt = stt or _RecordingSTT()

    audio_stage = AudioStage(nr, echo_canceller=aec if enable_aec else None)
    vad_stage = VADStage(vad)
    stt_stage = STTStage(stt)
    transport_stage = TransportStage(transport)

    tm = turn_manager or TurnManager(bus, config=TurnManagerConfig())
    no_turn = TurnContext(turn_id="no-turn", cancel_token=CancelToken())
    journal_sink = SessionJournalSink(
        event_bus=bus,
        journal=journal,
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
        "journal": journal,
        "runtime_scope": runtime_scope,
        "runtime_supervisor": runtime_supervisor,
    }

    wiring = make_wiring(
        enable_noise_reduction=lambda: enable_noise_reduction,
        enable_aec=lambda: enable_aec,
        enable_vad=lambda: enable_vad,
        auto_turn_from_stt_final=lambda: auto_turn_from_stt_final,
        emit=_emit,
        is_running=lambda: state["running"],
        set_running=lambda v: state.update(running=v),
        current_turn=lambda: state["current_turn"],
        is_stt_active=lambda: state["stt_active"],
    )
    router = AudioRouter(
        wiring=wiring,
        transport=transport,
        audio_stage=audio_stage,
        vad_stage=vad_stage,
        stt_stage=stt_stage,
        transport_stage=transport_stage,
        turn_manager=tm,
        event_bus=bus,
        journal_sink=journal_sink,
        runtime_scope=runtime_scope,
        run_ctx=run_ctx,
        no_turn=no_turn,
        echo_canceller=aec,
        outbound_queue=queue,
    )
    return router, state


# ── Tests ────────────────────────────────────────────────────


def test_inline_send_cohort_attaches_to_session_runtime_root() -> None:
    router, state = _make_router()
    inline_scope = router._inline_send_scope
    root = state["runtime_scope"]

    assert inline_scope.parent is root
    assert inline_scope.root is root
    assert inline_scope.owner_id == "session/audio-router-inline-send"
    assert inline_scope.survivor_registry is root.survivor_registry
    assert root.children() == (inline_scope,)


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
async def test_first_audio_sends_inline_only_when_outbound_path_is_idle():
    transport = _FakeTransport()
    router, state = _make_router(transport=transport)
    first = _make_chunk(byte_value=5)
    queued = _make_chunk(byte_value=6)

    # A live session without its outbound drain is not a valid direct-send
    # path (before Session.start or after outbound teardown).
    assert await router.try_send_first_audio_inline(first) is False

    hold_active = asyncio.Event()
    active_task = asyncio.create_task(hold_active.wait())
    router._outbound_task = active_task
    try:
        assert await router.try_send_first_audio_inline(first) is True
        assert transport.sent == [first]

        await router.queue_outbound(queued)
        assert await router.try_send_first_audio_inline(_make_chunk(byte_value=7)) is False
        assert transport.sent == [first]
        assert state["queue"].qsize() == 1
    finally:
        active_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active_task
        router._outbound_task = None


@pytest.mark.asyncio
async def test_nonblocking_transport_keeps_inline_send_in_caller_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ImmediateTransport(_FakeTransport):
        send_audio_is_nonblocking = True
        reports_audio_delivery = True

    transport = _ImmediateTransport()
    router, state = _make_router(transport=transport)
    router.start_outbound()

    def _unexpected_task(*args, **kwargs):
        _ = args, kwargs
        pytest.fail("nonblocking inline send should not create a child task")

    monkeypatch.setattr("easycat.session._audio_router.asyncio.create_task", _unexpected_task)

    assert await router.try_send_first_audio_inline(_make_chunk()) is True
    assert len(transport.sent) == 1
    assert router._outbound_in_flight == 0
    assert router._outbound_idle.is_set()

    state["running"] = False
    await router.stop_outbound()


@pytest.mark.asyncio
async def test_nonblocking_send_keeps_shield_when_delivery_handler_can_suspend() -> None:
    class _ImmediateTransport(_FakeTransport):
        send_audio_is_nonblocking = True

    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    handler_finished = asyncio.Event()
    transport = _ImmediateTransport()
    router, state = _make_router(transport=transport)

    async def _handle_audio_out(_event: AudioOut) -> None:
        handler_started.set()
        await release_handler.wait()
        handler_finished.set()

    state["bus"].subscribe(AudioOut, _handle_audio_out)
    router.start_outbound()

    inline = asyncio.create_task(router.try_send_first_audio_inline(_make_chunk()))
    await asyncio.wait_for(handler_started.wait(), timeout=1)
    inline.cancel()
    await asyncio.sleep(0)

    assert not inline.done()
    assert len(transport.sent) == 1
    assert router._outbound_in_flight == 1

    release_handler.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(inline, timeout=1)

    assert handler_finished.is_set()
    assert router._outbound_in_flight == 0

    state["running"] = False
    await router.stop_outbound()


@pytest.mark.asyncio
async def test_inline_send_defers_caller_cancellation_until_transport_finishes():
    started = asyncio.Event()
    release = asyncio.Event()
    transport_cancelled = False

    class _SlowTransport(_FakeTransport):
        async def send_audio(self, chunk: AudioChunk) -> bool:
            nonlocal transport_cancelled
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                transport_cancelled = True
                raise
            self.sent.append(chunk)
            return True

    transport = _SlowTransport()
    router, state = _make_router(transport=transport)
    router.start_outbound()

    inline = asyncio.create_task(router.try_send_first_audio_inline(_make_chunk()))
    await asyncio.wait_for(started.wait(), timeout=1)
    inline.cancel()
    await asyncio.sleep(0)

    assert not inline.done()
    assert not transport_cancelled
    assert router._outbound_in_flight == 1

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(inline, timeout=1)

    assert not transport_cancelled
    assert len(transport.sent) == 1
    assert router._outbound_in_flight == 0
    assert router._outbound_idle.is_set()

    state["running"] = False
    await router.stop_outbound()


@pytest.mark.asyncio
async def test_inline_send_keeps_stubborn_transport_owned_until_it_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    transport_cancelled = asyncio.Event()
    transport_disconnected = asyncio.Event()
    release = asyncio.Event()

    class _WedgedTransport(_FakeTransport):
        async def send_audio(self, chunk: AudioChunk) -> bool:
            _ = chunk
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                transport_cancelled.set()
                await release.wait()
            return True

        async def disconnect(self) -> None:
            transport_disconnected.set()

    monkeypatch.setattr(AudioRouter, "_INLINE_SEND_TIMEOUT_S", 0.02)
    monkeypatch.setattr(AudioRouter, "_INLINE_SEND_CANCEL_GRACE_S", 0.02)
    router, state = _make_router(transport=_WedgedTransport())
    router.start_outbound()

    inline = asyncio.create_task(router.try_send_first_audio_inline(_make_chunk()))
    await asyncio.wait_for(started.wait(), timeout=1)
    inline.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(inline, timeout=1)

    assert transport_cancelled.is_set()
    assert transport_disconnected.is_set()
    assert router._outbound_in_flight == 1
    assert router._outbound_send_lock.locked()
    assert not router._outbound_idle.is_set()
    assert state["runtime_scope"].tasks("audio_inline_send")
    assert state["runtime_supervisor"].active_count == 1

    state["running"] = False
    stopping = asyncio.create_task(router.stop_outbound())
    await asyncio.sleep(0)

    assert not stopping.done()

    release.set()
    await asyncio.wait_for(stopping, timeout=1)
    assert router._outbound_in_flight == 0
    assert not router._outbound_send_lock.locked()
    assert router._outbound_idle.is_set()
    assert state["runtime_supervisor"].active_count == 0


@pytest.mark.asyncio
async def test_force_drain_parks_stubborn_inline_send_at_hard_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class _CancellationResistantTransport(_FakeTransport):
        async def send_audio(self, chunk: AudioChunk) -> bool:
            _ = chunk
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            return True

    monkeypatch.setattr(AudioRouter, "_INLINE_SEND_TIMEOUT_S", 0.01)
    monkeypatch.setattr(AudioRouter, "_INLINE_SEND_CANCEL_GRACE_S", 0.01)
    router, state = _make_router(transport=_CancellationResistantTransport())
    router.start_outbound()
    inline = asyncio.create_task(router.try_send_first_audio_inline(_make_chunk()))

    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        scope = state["runtime_scope"]
        signal = scope.signal_cohort("audio-inline-send", force=True)
        await asyncio.wait_for(scope.drain_cohort(signal), timeout=1)

        [owned_send] = scope.tasks("audio_inline_send")
        assert not owned_send.done()
        assert state["runtime_supervisor"].survivor_count == 1
        assert router._outbound_in_flight == 1

        await asyncio.wait_for(router.stop_outbound(force=True), timeout=0.1)
        assert not owned_send.done()
        assert state["runtime_supervisor"].survivor_count == 1

        inline.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(inline, timeout=1)

        release.set()
        await asyncio.wait_for(owned_send, timeout=1)
        await asyncio.sleep(0)

        assert scope.tasks("audio_inline_send") == ()
        assert state["runtime_supervisor"].survivor_count == 0
        assert router._outbound_in_flight == 0
        assert router._outbound_idle.is_set()
    finally:
        release.set()
        if not inline.done():
            inline.cancel()
        await asyncio.gather(inline, return_exceptions=True)
        state["running"] = False
        await router.stop_outbound()


@pytest.mark.asyncio
async def test_inline_send_quota_rejection_releases_outbound_claim() -> None:
    router, state = _make_router()
    router.start_outbound()
    release = asyncio.Event()
    blocker = await router._inline_send_scope.start_owned_task("blocker", release.wait)

    assert await router.try_send_first_audio_inline(_make_chunk()) is False

    assert router._outbound_in_flight == 0
    assert router._outbound_idle.is_set()

    release.set()
    await blocker
    await router._inline_send_scope.drain()
    state["running"] = False
    await router.stop_outbound()


@pytest.mark.asyncio
async def test_inline_send_has_no_router_deadline_without_caller_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class _BackpressuredTransport(_FakeTransport):
        async def send_audio(self, chunk: AudioChunk) -> bool:
            _ = chunk
            started.set()
            await release.wait()
            return True

    monkeypatch.setattr(AudioRouter, "_INLINE_SEND_TIMEOUT_S", 0.02)
    router, state = _make_router(transport=_BackpressuredTransport())
    router.start_outbound()

    inline = asyncio.create_task(router.try_send_first_audio_inline(_make_chunk()))
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(0.04)

    assert not inline.done()
    assert router._outbound_in_flight == 1

    release.set()
    assert await asyncio.wait_for(inline, timeout=1) is True
    assert router._outbound_in_flight == 0
    assert router._outbound_idle.is_set()

    state["running"] = False
    await router.stop_outbound()


@pytest.mark.asyncio
async def test_inline_send_keeps_turn_when_shield_task_starts_late(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_turn = TurnContext(turn_id="original", cancel_token=CancelToken())
    next_turn = TurnContext(turn_id="next", cancel_token=CancelToken())
    transport = _FakeTransport()
    router, state = _make_router(transport=transport, current_turn=original_turn)
    hold_active = asyncio.Event()
    active_task = asyncio.create_task(hold_active.wait())
    router._outbound_task = active_task

    real_create_task = asyncio.create_task
    child_created = asyncio.Event()
    release_child = asyncio.Event()

    def _delayed_create_task(coro, *, name=None, **kwargs):
        async def _run_later():
            child_created.set()
            await release_child.wait()
            return await coro

        return real_create_task(_run_later(), name=name, **kwargs)

    monkeypatch.setattr(
        "easycat.session._audio_router.asyncio.create_task",
        _delayed_create_task,
    )
    chunk = _make_chunk()
    inline = real_create_task(router.try_send_first_audio_inline(chunk))

    try:
        await asyncio.wait_for(child_created.wait(), timeout=1)
        state["current_turn"] = next_turn
        release_child.set()

        assert await asyncio.wait_for(inline, timeout=1) is True
        assert chunk._easycat_turn_ref is original_turn
        assert chunk._easycat_turn_id == "original"
        audio_outs = [evt for evt in state["emitted"] if isinstance(evt, AudioOut)]
        assert [evt.turn_id for evt in audio_outs] == ["original"]
    finally:
        release_child.set()
        if not inline.done():
            inline.cancel()
            with pytest.raises(asyncio.CancelledError):
                await inline
        active_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active_task
        router._outbound_task = None


@pytest.mark.asyncio
async def test_dequeued_chunk_is_claimed_before_waiting_for_send_lock():
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    release_second = asyncio.Event()

    class _ContendedTransport(_FakeTransport):
        async def send_audio(self, chunk: AudioChunk) -> bool:
            if not self.sent:
                first_started.set()
                await release_first.wait()
            else:
                second_started.set()
                await release_second.wait()
            self.sent.append(chunk)
            return True

    transport = _ContendedTransport()
    router, state = _make_router(transport=transport)
    router.start_outbound()

    inline = asyncio.create_task(router.try_send_first_audio_inline(_make_chunk(byte_value=1)))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await router.queue_outbound(_make_chunk(byte_value=2))
    for _ in range(20):
        if state["queue"].empty() and router._outbound_in_flight == 2:
            break
        await asyncio.sleep(0)

    # The inline send holds the lock; the drain has dequeued the second chunk.
    # Both must count as in flight before either transport send completes.
    assert state["queue"].empty()
    assert router._outbound_in_flight == 2
    assert not router._outbound_idle.is_set()

    release_first.set()
    assert await asyncio.wait_for(inline, timeout=1) is True
    await asyncio.wait_for(second_started.wait(), timeout=1)
    assert router._outbound_in_flight == 1
    assert not router._outbound_idle.is_set()

    release_second.set()
    await router.await_drain(timeout=1)
    assert len(transport.sent) == 2

    state["running"] = False
    await router.stop_outbound()


@pytest.mark.asyncio
async def test_dequeued_chunk_keeps_turn_while_waiting_for_send_lock() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class _ContendedTransport(_FakeTransport):
        async def send_audio(self, chunk: AudioChunk) -> bool:
            if not self.sent:
                first_started.set()
                await release_first.wait()
            self.sent.append(chunk)
            return True

    original_turn = TurnContext(turn_id="original", cancel_token=CancelToken())
    next_turn = TurnContext(turn_id="next", cancel_token=CancelToken())
    transport = _ContendedTransport()
    router, state = _make_router(transport=transport, current_turn=original_turn)
    router.start_outbound()
    first = _make_chunk(byte_value=1)
    second = _make_chunk(byte_value=2)

    inline = asyncio.create_task(router.try_send_first_audio_inline(first))
    try:
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await router.queue_outbound(second)
        for _ in range(20):
            if state["queue"].empty() and router._outbound_in_flight == 2:
                break
            await asyncio.sleep(0)

        assert state["queue"].empty()
        assert router._outbound_in_flight == 2
        state["current_turn"] = next_turn
        release_first.set()

        assert await asyncio.wait_for(inline, timeout=1) is True
        await router.await_drain(timeout=1)
        assert second._easycat_turn_ref is original_turn
        assert second._easycat_turn_id == "original"
        audio_outs = [evt for evt in state["emitted"] if isinstance(evt, AudioOut)]
        assert [evt.turn_id for evt in audio_outs] == ["original", "original"]
    finally:
        release_first.set()
        state["running"] = False
        await router.stop_outbound()


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
async def test_gated_replay_overflow_reconciles_pending_count_after_drain():
    queue = BoundedAudioQueue(max_size=3, name="test_outbound")
    transport = _FakeTransport()
    router, state = _make_router(transport=transport, outbound_queue=queue)
    state["running"] = False
    events = [TTSAudio(chunk=_make_chunk(byte_value=i + 1)) for i in range(5)]

    await router.gated_replay(events)

    assert state["tm"].state == TurnManagerState.BOT_SPEAKING
    assert queue.qsize() == 3
    assert queue.drops == 2
    assert router._replay_chunks_pending == 5

    await router._drain_outbound_audio()

    assert len(transport.sent) == 3
    assert queue.empty()
    assert router._replay_chunks_pending == 0
    assert state["tm"].state == TurnManagerState.IDLE


@pytest.mark.asyncio
async def test_gated_replay_overflow_reconciles_when_non_replay_chunk_drains_last():
    queue = BoundedAudioQueue(max_size=3, name="test_outbound")
    transport = _FakeTransport()
    router, state = _make_router(transport=transport, outbound_queue=queue)
    state["running"] = False
    events = [TTSAudio(chunk=_make_chunk(byte_value=i + 1)) for i in range(5)]

    await router.gated_replay(events)
    live_chunk = _make_chunk(byte_value=99)
    await queue.put(live_chunk)

    assert state["tm"].state == TurnManagerState.BOT_SPEAKING
    assert queue.qsize() == 3
    assert queue.drops == 3
    assert router._replay_chunks_pending == 5

    await router._drain_outbound_audio()

    assert transport.sent[-1] is live_chunk
    assert queue.empty()
    assert router._replay_chunks_pending == 0
    assert state["tm"].state == TurnManagerState.IDLE


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


class _RaisingAEC:
    """AEC whose feed_reference rejects a far/near sample-rate mismatch."""

    def __init__(self) -> None:
        self.feed_calls = 0

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk

    def feed_reference(self, chunk: AudioChunk) -> None:
        self.feed_calls += 1
        raise ValueError("AEC near-end and far-end sample rates must match")

    def configure(self, **kwargs) -> None:
        pass


@pytest.mark.asyncio
async def test_aec_reference_failure_does_not_abort_delivery():
    """A feed_reference rate mismatch must not be mislabeled as a transport
    send failure, must not suppress AudioOut, and must latch off after the
    first failure rather than re-attempt (and re-log) every chunk."""
    transport = _FakeTransport()
    turn = TurnContext(turn_id="t", cancel_token=CancelToken())
    router, state = _make_router(transport=transport, current_turn=turn, enable_aec=True)
    aec = _RaisingAEC()
    router._echo_canceller = aec
    state["running"] = False  # exit drain once the queue empties

    await router.queue_outbound(_make_chunk(byte_value=3))
    await router.queue_outbound(_make_chunk(byte_value=4))
    await router._drain_outbound_audio()

    # Both chunks reach the transport; neither send is treated as a failure.
    assert len(transport.sent) == 2
    assert router._outbound_send_failures == 0
    # AudioOut is still emitted for delivered chunks.
    audio_outs = [evt for evt in state["emitted"] if isinstance(evt, AudioOut)]
    assert len(audio_outs) == 2
    # No bus-level Error was raised about the bot's audio being dropped.
    assert not [evt for evt in state["emitted"] if isinstance(evt, Error)]
    # The reference feed latches off after the first failure.
    assert router._aec_reference_failed is True
    assert aec.feed_calls == 1


@pytest.mark.asyncio
async def test_start_and_stop_ingress_cancels_task():
    ingress_started = asyncio.Event()
    never_released = asyncio.Event()

    # Transport that never yields so the loop blocks until cancelled.
    class _StalledTransport(_FakeTransport):
        async def receive_audio(self) -> AsyncIterator[AudioChunk]:
            ingress_started.set()
            await never_released.wait()
            if False:
                yield None

    journal = InMemoryRingBuffer(capacity=64)
    router, state = _make_router(transport=_StalledTransport(), journal=journal)
    task = router.start_ingress()
    assert router.pipeline_task is task
    assert task in state["runtime_scope"].tasks(AudioRouter._INGRESS_TASK_NAME)
    await asyncio.wait_for(ingress_started.wait(), timeout=1.0)
    await router.stop_ingress()
    assert router.pipeline_task is None
    assert task.cancelled() or task.done()
    assert not state["runtime_scope"].tasks(AudioRouter._INGRESS_TASK_NAME)
    records = [
        record
        for record in journal.read()
        if record.data.get("task_name") == AudioRouter._INGRESS_TASK_NAME
    ]
    assert [record.name for record in records] == ["task_scheduled", "task_completed"]


@pytest.mark.asyncio
async def test_stop_ingress_from_ingress_task_detaches_scope():
    journal = InMemoryRingBuffer(capacity=64)
    router, state = _make_router(
        transport=_FakeTransport(chunks=[_make_chunk()]),
        journal=journal,
    )

    async def _stop_from_audio_in(_event: AudioIn) -> None:
        await router.stop_ingress()

    state["bus"].subscribe(AudioIn, _stop_from_audio_in)

    task = router.start_ingress()
    await task

    assert router.pipeline_task is None
    assert not state["runtime_scope"].tasks(AudioRouter._INGRESS_TASK_NAME)
    records = [
        record
        for record in journal.read()
        if record.data.get("task_name") == AudioRouter._INGRESS_TASK_NAME
    ]
    assert [record.name for record in records] == ["task_scheduled", "task_completed"]


@pytest.mark.asyncio
async def test_start_outbound_tracks_and_drains_task():
    journal = InMemoryRingBuffer(capacity=64)
    transport = _FakeTransport()
    router, state = _make_router(transport=transport, journal=journal)
    state["running"] = False

    await router.queue_outbound(_make_chunk(byte_value=5))
    task = router.start_outbound()
    assert router.outbound_task is task
    assert task in state["runtime_scope"].tasks(AudioRouter._OUTBOUND_TASK_NAME)

    await task
    await router.stop_outbound()

    assert transport.sent
    assert router.outbound_task is None
    assert not state["runtime_scope"].tasks(AudioRouter._OUTBOUND_TASK_NAME)
    records = [
        record
        for record in journal.read()
        if record.data.get("task_name") == AudioRouter._OUTBOUND_TASK_NAME
    ]
    assert [record.name for record in records] == ["task_scheduled", "task_completed"]


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
    # Exactly one Error per failed frame, including the threshold frame that
    # tears the session down: the fatal frame surfaces a single Error (no
    # duplicate from the outer handler), so the count is the threshold, not
    # threshold + 1.
    assert len(errors) == threshold
    # The session is torn down once the threshold is hit.
    assert state["running"] is False


@pytest.mark.asyncio
async def test_batch_stt_buffer_cap_does_not_tear_down_session():
    """A long-talking caller hitting the batch buffer cap keeps the call alive.

    Regression for PR #167: the batch buffer cap used to raise a per-chunk
    ``ValueError`` for every frame past the cap. Driving STT continuously
    (as a long-talking caller does) then accumulated consecutive per-chunk
    errors and tripped ``_MAX_CONSECUTIVE_CHUNK_ERRORS``, tearing down the
    whole live call. The cap now finalizes the current utterance gracefully
    instead, so no Error is surfaced and the pipeline survives.
    """
    from unittest.mock import AsyncMock, MagicMock

    import httpx

    from easycat.stt.openai_provider import OpenAISTT, OpenAISTTConfig

    # Mock OpenAI streaming transcription so the early finalize stays offline.
    class _MockStreamResponse:
        request = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self):
            body = 'data: {"text": "partial", "is_final": true}\ndata: [DONE]\n'
            yield body.encode("utf-8")

    class _MockStreamCtx:
        async def __aenter__(self):
            return _MockStreamResponse()

        async def __aexit__(self, *exc):
            return None

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.stream = MagicMock(return_value=_MockStreamCtx())
    mock_client.aclose = AsyncMock()

    # Tiny caps so a handful of normal frames trips the buffer cap repeatedly.
    provider = OpenAISTT(
        OpenAISTTConfig(
            api_key="test-key",
            max_audio_chunk_bytes=10_000,
            max_audio_buffer_bytes=512,
            http_client=mock_client,
        )
    )
    await provider.start_stream()

    threshold = AudioRouter._MAX_CONSECUTIVE_CHUNK_ERRORS
    # Each chunk is 320 bytes, so two chunks already exceed the 512-byte cap;
    # send well past the consecutive-error threshold to prove no streak forms.
    chunks = [_make_chunk(n_samples=160) for _ in range(threshold + 10)]
    transport = _FakeTransport(chunks=chunks)
    turn = TurnContext(turn_id="t1", cancel_token=CancelToken())
    router, state = _make_router(
        transport=transport,
        is_stt_active=True,
        current_turn=turn,
        stt=provider,
    )

    await router._run_pipeline()

    # The cap was hit many times but never surfaced as a pipeline Error, so
    # the consecutive-error counter never tripped the teardown sentinel.
    errors = [evt for evt in state["emitted"] if isinstance(evt, Error)]
    assert errors == []
    # The pipeline ran to natural transport exhaustion (running flips to False
    # only in the finally block on normal exit), not a forced teardown — and
    # the buffered utterances were finalized via the transcription path.
    assert mock_client.stream.call_count >= 1


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


class _DrainRefTransport(_FakeTransport):
    """Delivery-reporting transport that exposes an AEC reference drain queue.

    Mirrors LocalTransport / the WebRTC outbound source: the output callback
    accumulates far-end (speaker) frames at playback time and the router drains
    them via ``drain_aec_reference_frames()`` before the near-end mic frame.
    """

    reports_audio_delivery = True

    def __init__(self, ref_frames: list[AudioChunk | bytes]) -> None:
        super().__init__()
        self._ref_frames = ref_frames
        self.drain_calls = 0

    def drain_aec_reference_frames(self) -> list[AudioChunk | bytes]:
        self.drain_calls += 1
        if self.drain_calls > 1:
            return []
        return list(self._ref_frames)


@pytest.mark.asyncio
async def test_drain_path_journals_reference_and_feeds_before_near_end():
    """Issue B + drain-path ordering.

    A transport that exposes ``drain_aec_reference_frames()`` feeds the far-end
    reference in ``_process_chunk`` *before* ``AudioStage.execute`` runs the
    near-end mic frame.  Even though ``_handle_audio_delivery`` skips the feed
    for these transports, the far-end reference must still be journaled on the
    drain feed path (it is the one AEC leg the pipeline never journals itself).
    """
    import dataclasses

    from easycat.runtime.artifacts import InMemoryArtifactStore
    from easycat.runtime.records import AEC_REFERENCE_FRAME_NAME

    ref_frames = [b"\x01\x02" * 80, b"\x03\x04" * 80]
    transport = _DrainRefTransport(ref_frames)
    turn = TurnContext(turn_id="t", cancel_token=CancelToken())
    journal = InMemoryRingBuffer(capacity=64)
    router, state = _make_router(
        transport=transport,
        enable_aec=True,
        current_turn=turn,
        journal=journal,
    )
    # The router only journals the reference when capture is opted in *and* an
    # artifact store is present; wire both so the drain feed path can journal.
    router._capture_aec_reference = True
    router._run_ctx = dataclasses.replace(
        router._run_ctx,
        journal=journal,
        artifact_store=InMemoryArtifactStore(),
    )

    # The router is constructed with a drain-capable transport, so it routes the
    # reference through the drain path rather than _handle_audio_delivery.
    assert router._transport_has_aec_drain is True

    # Record relative ordering of the far-end feed vs. the near-end audio stage.
    order: list[str] = []
    aec = router._echo_canceller
    orig_feed = aec.feed_reference

    def _spy_feed(chunk):
        order.append("feed")
        orig_feed(chunk)

    aec.feed_reference = _spy_feed

    audio_stage = state["audio_stage"]
    orig_execute = audio_stage.execute

    async def _spy_execute(chunk, ctx, t):
        order.append("execute")
        return await orig_execute(chunk, ctx, t)

    audio_stage.execute = _spy_execute

    await router._process_chunk(_make_chunk())

    # Both drained reference frames were fed, and every feed landed before the
    # near-end mic frame reached AudioStage.execute.
    assert order.count("feed") == len(ref_frames)
    assert order.count("execute") == 1
    assert order[-1] == "execute"
    assert all(marker == "feed" for marker in order[:-1])

    # The far-end reference IS journaled on the drain feed path (decimated to
    # the first frame of the window via _AEC_REFERENCE_CAPTURE_EVERY_N_FRAMES).
    ref_records = [r for r in journal.read() if r.name == AEC_REFERENCE_FRAME_NAME]
    assert len(ref_records) == 1


@pytest.mark.asyncio
async def test_drain_path_preserves_reference_format_instead_of_using_mic_format():
    """A typed WebRTC reference must reach AEC with its true far-end rate.

    Advanced configurations can intentionally disable TTS/transport alignment.
    Preserving 24 kHz here lets LiveKitAEC's existing 24 kHz-vs-16 kHz guard
    reject the mismatch instead of processing mislabeled PCM.
    """
    reference = AudioChunk(data=b"\x01\x02" * 240, format=PCM16_MONO_24K)
    transport = _DrainRefTransport([reference])
    router, _ = _make_router(transport=transport, enable_aec=True)
    fed: list[AudioChunk] = []
    router._echo_canceller.feed_reference = fed.append

    await router._process_chunk(_make_chunk())

    assert fed == [reference]
    assert fed[0].format == PCM16_MONO_24K


@pytest.mark.asyncio
async def test_send_time_aec_reference_reports_degradation_once_to_journal():
    """Explicit AEC on a no-drain transport remains available but observable."""

    class _WebSocketLikeTransport(_FakeTransport):
        transport_kind = "websocket"

    journal = InMemoryRingBuffer(capacity=64)
    router, state = _make_router(
        transport=_WebSocketLikeTransport(),
        enable_aec=True,
        journal=journal,
    )
    router._journal_sink.subscribe()

    await router._handle_audio_delivery(_make_chunk(byte_value=3), None)
    await router._handle_audio_delivery(_make_chunk(byte_value=4), None)
    await state["runtime_scope"].drain("aec_reference_degraded_emit")

    degraded = [event for event in state["emitted"] if isinstance(event, TransportDegraded)]
    assert len(degraded) == 1
    assert degraded[0].provider == "websocket"
    assert degraded[0].reason == "aec_reference_degraded"
    records = [record for record in journal.read() if record.name == "transport_degraded"]
    assert len(records) == 1
    assert records[0].data["reason"] == "aec_reference_degraded"


@pytest.mark.asyncio
async def test_slow_degraded_handler_does_not_stall_audio_delivery() -> None:
    class _WebSocketLikeTransport(_FakeTransport):
        transport_kind = "websocket"

    turn = TurnContext(turn_id="delivery", cancel_token=CancelToken())
    router, state = _make_router(
        transport=_WebSocketLikeTransport(),
        enable_aec=True,
        current_turn=turn,
    )
    handler_started = asyncio.Event()
    handler_release = asyncio.Event()

    async def slow_handler(_event: TransportDegraded) -> None:
        handler_started.set()
        await handler_release.wait()

    state["bus"].subscribe(TransportDegraded, slow_handler)

    await asyncio.wait_for(
        router._handle_audio_delivery(_make_chunk(byte_value=3), turn),
        timeout=0.1,
    )
    await asyncio.wait_for(handler_started.wait(), timeout=0.1)

    assert turn.audio_bytes_sent > 0
    assert state["runtime_scope"].tasks("aec_reference_degraded_emit")

    handler_release.set()
    await state["runtime_scope"].drain("aec_reference_degraded_emit")


@pytest.mark.asyncio
async def test_await_drain_extends_playout_deadline_beyond_fixed_timeout(monkeypatch):
    """Issue G.

    The local output queue can buffer far more than the fixed ``timeout``
    seconds of audio, so ``await_drain`` must derive its playout deadline from
    the transport's reported ``pending_playout_ms`` instead of bailing after the
    fixed ``timeout`` while the speaker buffer is still draining.

    A fake monotonic clock keeps the test deterministic and fast: no real
    multi-second sleep occurs.
    """

    class _Clock:
        def __init__(self, start: float) -> None:
            self.now = start

        def time(self) -> float:
            return self.now

    clock = _Clock(1000.0)
    start = clock.now
    # Backlog (5s) far exceeds the 2s fixed timeout; the speaker buffer reports
    # drained only once the (fake) clock passes 4s — past the fixed timeout.
    drain_at = start + 4.0

    class _PlayoutTransport(_FakeTransport):
        def pending_playout_ms(self) -> float:
            return 0.0 if clock.now >= drain_at else 5000.0

    transport = _PlayoutTransport()
    router, _state = _make_router(transport=transport)

    real_sleep = asyncio.sleep

    async def _advancing_sleep(delay: float) -> None:
        clock.now += delay
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: clock)
    monkeypatch.setattr(asyncio, "sleep", _advancing_sleep)

    # No outbound task in flight and the queue is empty, so await_drain goes
    # straight to the playout wait. With the fixed-2s deadline this would bail
    # at clock.now == start + 2.0 while pending is still 5000ms; the
    # pending-derived deadline keeps waiting until the buffer truly empties.
    await router.await_drain(timeout=2.0)

    # await_drain waited past the fixed 2s timeout and returned only once the
    # speaker buffer actually drained (~4s of simulated playout).
    assert transport.pending_playout_ms() == 0.0
    assert clock.now - start >= 4.0
    # ...and did not run away past the pending-derived deadline (5s + 0.5 margin).
    assert clock.now - start <= 5.5 + 0.05


@pytest.mark.asyncio
async def test_await_drain_waits_for_transport_playout_buffer():
    """A transport exposing pending_playout_ms is waited on until it empties."""

    class _PlayoutTransport(_FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.pending_ms = 40.0

        def pending_playout_ms(self) -> float:
            return self.pending_ms

    transport = _PlayoutTransport()
    router, state = _make_router(transport=transport)

    # No outbound work in flight and the queue is empty, but the local speaker
    # buffer still reports pending audio: await_drain must not return early.
    router.start_outbound()
    state["running"] = False
    await asyncio.sleep(0)

    # Playout buffer never empties within the window: bounded by the timeout.
    await router.await_drain(timeout=0.05)
    assert transport.pending_ms == 40.0  # still pending; await_drain bailed on timeout

    # Once the speaker buffer drains, await_drain returns promptly.
    transport.pending_ms = 0.0
    await router.await_drain(timeout=1.0)

    await router.stop_outbound()


@pytest.mark.asyncio
async def test_await_drain_is_noop_without_playout_hook():
    """Transports lacking pending_playout_ms keep the original drained semantics."""
    transport = _FakeTransport()
    router, state = _make_router(transport=transport)
    # No hook present: with an empty queue and nothing in flight, await_drain
    # must return immediately (strict no-op for the playout extension).
    assert not hasattr(transport, "pending_playout_ms")
    router.start_outbound()
    state["running"] = False
    await asyncio.sleep(0)

    await router.await_drain(timeout=0.05)

    await router.stop_outbound()
