"""Tests for Session lifecycle, cancellation, pipeline, and CancelToken."""

import asyncio
import zipfile
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.bounded_queue import BoundedAudioQueue
from easycat.cancel import CancelToken
from easycat.events import (
    AgentDelta,
    AgentFinal,
    AudioIn,
    AudioOut,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    Error,
    ErrorStage,
    Event,
    Interruption,
    PlaybackMarkAck,
    STTEvent,
    STTEventType,
    STTFinal,
    ToolCallResult,
    ToolCallStarted,
    TransportAudioDelivered,
    TTSAudio,
    TTSEvent,
    TTSEventType,
    TurnEnded,
    TurnStarted,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.llm_output_processing import (
    PauseProcessor,
    PhoneticReplacementProcessor,
)
from easycat.noise_reduction import PassthroughNoiseReducer
from easycat.runtime.artifacts import FilesystemArtifactStore, InMemoryArtifactStore
from easycat.runtime.journal import InMemoryRingBuffer, SqliteJournal
from easycat.runtime.records import JournalRecordKind
from easycat.session._session import Session
from easycat.session._turn_context import TurnContext
from easycat.session._types import SessionConfig, TurnState
from easycat.timeouts import AgentTimeoutError
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManagerConfig, TurnManagerState

# ── Test helpers ───────────────────────────────────────────────────


def _make_chunk(n_bytes: int = 320) -> AudioChunk:
    return AudioChunk(data=bytes(n_bytes), format=PCM16_MONO_16K)


def _make_loud_chunk(n_samples: int = 160, amplitude: int = 6000) -> AudioChunk:
    sample = int(amplitude).to_bytes(2, "little", signed=True)
    return AudioChunk(data=sample * n_samples, format=PCM16_MONO_16K)


class FakeTransport:
    def __init__(self, chunks: list[AudioChunk] | None = None) -> None:
        self.chunks = chunks or []
        self.sent: list[AudioChunk] = []
        self.connected = False
        self.disconnected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        for chunk in self.chunks:
            yield chunk

    async def send_audio(self, chunk: AudioChunk) -> bool:
        self.sent.append(chunk)
        return True

    async def clear_audio(self) -> None:
        pass


class FakePlaybackAckTransport(FakeTransport):
    def __init__(self, chunks: list[AudioChunk] | None = None) -> None:
        super().__init__(chunks=chunks)
        self.playback_marks: list[str] = []

    async def send_playback_mark(self, name: str | None = None) -> str:
        mark_name = name or f"mark_{len(self.playback_marks) + 1}"
        self.playback_marks.append(mark_name)
        return mark_name


class ReportingTransport(FakeTransport):
    reports_audio_delivery = True


class FakeVAD:
    def __init__(self) -> None:
        self._call_count = 0

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        self._call_count += 1
        if self._call_count == 1:
            yield VADStartSpeaking()
        elif self._call_count == 2:
            yield VADStopSpeaking()

    def configure(self, **kwargs: object) -> None:
        pass


class FakeSTT:
    """STT that uses provider-scoped STTEvent via events() iterator."""

    def __init__(self, transcript: str = "hello world") -> None:
        self._transcript = transcript
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()

    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def end_stream(self) -> None:
        if self._transcript:
            await self._queue.put(STTEvent(type=STTEventType.FINAL, text=self._transcript))
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event


class SegmentingSTT:
    """STT that supports early segment commits within a single stream."""

    def __init__(self, committed_segments: list[str], final_segment: str = "") -> None:
        self._committed_segments = list(committed_segments)
        self._final_segment = final_segment
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self.commit_calls = 0
        self.start_calls = 0
        self.end_calls = 0

    async def start_stream(self) -> None:
        self.start_calls += 1
        self._queue = asyncio.Queue()

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def commit_segment(self) -> bool:
        if not self._committed_segments:
            return False
        self.commit_calls += 1
        await self._queue.put(
            STTEvent(type=STTEventType.FINAL, text=self._committed_segments.pop(0))
        )
        return True

    async def end_stream(self) -> None:
        self.end_calls += 1
        if self._final_segment:
            await self._queue.put(STTEvent(type=STTEventType.FINAL, text=self._final_segment))
            self._final_segment = ""
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event


class AutoTurnSTT(FakeSTT):
    def __init__(self, transcript: str = "hello flux", *, final_after_chunks: int = 3) -> None:
        super().__init__(transcript=transcript)
        self.final_after_chunks = final_after_chunks
        self.sent_chunks: list[AudioChunk] = []
        self.start_count = 0
        self.end_count = 0
        self._final_emitted = False

    async def start_stream(self) -> None:
        self.start_count += 1

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent_chunks.append(chunk)
        if not self._final_emitted and len(self.sent_chunks) >= self.final_after_chunks:
            self._final_emitted = True
            await self._queue.put(STTEvent(type=STTEventType.FINAL, text=self._transcript))

    async def end_stream(self) -> None:
        self.end_count += 1
        await self._queue.put(None)


class FakeAgent:
    async def run(self, text: str) -> str:
        return text.upper()


class FakeTTS:
    """TTS that uses provider-scoped TTSEvent."""

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(
            type=TTSEventType.AUDIO,
            audio=_make_chunk(),
        )

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


class MarkerTTS(FakeTTS):
    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(type=TTSEventType.MARKERS, markers=[{"word": payload.text, "start_ms": 0}])
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_make_chunk())


class TrackingJournal:
    def __init__(self) -> None:
        self.finalize_calls = 0
        self.close_calls = 0

    def append(
        self,
        kind: JournalRecordKind,
        name: str,
        session_id: str,
        turn_id: str | None = None,
        data: dict[str, object] | None = None,
        error: object | None = None,
        tags: frozenset[str] = frozenset(),
        input_ref: str | None = None,
        output_ref: str | None = None,
    ) -> int:
        return 1

    def read(self, start: int = 0, limit: int | None = None) -> list[object]:
        return []

    def slice(
        self,
        *,
        kind: JournalRecordKind | None = None,
        session_id: str | None = None,
    ) -> list[object]:
        return []

    def close(self) -> None:
        self.close_calls += 1

    def flush(self) -> None:
        pass

    def finalize(self) -> None:
        self.finalize_calls += 1

    @property
    def latest_sequence(self) -> int:
        return 0

    @property
    def degraded(self) -> bool:
        return False


_FAST_TURN = TurnManagerConfig(end_of_turn_silence_ms=1)


def _full_config(**overrides) -> SessionConfig:
    """Build a SessionConfig with all required providers filled in."""
    defaults = dict(
        transport=FakeTransport(),
        vad=FakeVAD(),
        stt=FakeSTT(),
        agent=FakeAgent(),
        tts=FakeTTS(),
        noise_reducer=PassthroughNoiseReducer(),
        enable_noise_reduction=False,
    )
    defaults.update(overrides)
    return SessionConfig(**defaults)


# ── CancelToken tests ──────────────────────────────────────────────


def test_cancel_token_initial_state():
    token = CancelToken()
    assert not token.is_cancelled


def test_cancel_token_cancel():
    token = CancelToken()
    token.cancel()
    assert token.is_cancelled


@pytest.mark.asyncio
async def test_cancel_token_wait():
    token = CancelToken()

    async def cancel_later():
        await asyncio.sleep(0.01)
        token.cancel()

    asyncio.create_task(cancel_later())
    await token.wait()
    assert token.is_cancelled


# ── Session lifecycle tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_default_construction():
    session = Session(_full_config())
    assert session.turn_state == TurnState.IDLE
    assert not session.is_running
    assert session.cancel_token is None


@pytest.mark.asyncio
async def test_session_start_and_stop():
    transport = FakeTransport()
    config = _full_config(transport=transport)
    session = Session(config)

    await session.start()
    assert session.is_running
    assert transport.connected

    await session.stop()
    assert not session.is_running
    assert transport.disconnected
    assert session.turn_state == TurnState.IDLE


def test_session_strip_markdown_does_not_inject_hidden_processor():
    class MarkerProcessor:
        def process(self, payload: TTSInput, *, is_final: bool, is_streaming: bool) -> TTSInput:
            return payload

    processor = MarkerProcessor()
    session = Session(_full_config(strip_markdown=True, output_processors=[processor]))

    assert session._tts_scheduler._output_processors == [processor]


@pytest.mark.asyncio
async def test_session_shutdown():
    transport = FakeTransport()
    config = _full_config(transport=transport)
    session = Session(config)

    await session.start()
    await session.shutdown()

    assert not session.is_running
    assert transport.disconnected


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["stop", "shutdown"])
async def test_session_teardown_finalizes_and_closes_journal(method_name: str):
    transport = FakeTransport()
    journal = TrackingJournal()
    session = Session(_full_config(transport=transport, journal=journal, session_id="sess"))

    await session.start()
    await getattr(session, method_name)()

    assert journal.finalize_calls == 1
    assert journal.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["stop", "shutdown"])
async def test_session_teardown_closes_audio_providers(method_name: str):
    calls: list[str] = []

    class CloseSTT(FakeSTT):
        def close(self) -> None:
            calls.append("stt.close")

    class AsyncCloseTTS(FakeTTS):
        async def aclose(self) -> None:
            calls.append("tts.aclose")

    class AsyncCloseVAD(FakeVAD):
        async def close(self) -> None:
            calls.append("vad.close")

    class CloseNoiseReducer:
        async def process(self, chunk: AudioChunk) -> AudioChunk:
            return chunk

        def close(self) -> None:
            calls.append("noise.close")

    class AsyncCloseEchoCanceller:
        async def process(self, chunk: AudioChunk) -> AudioChunk:
            return chunk

        def feed_reference(self, chunk: AudioChunk) -> None:
            pass

        async def aclose(self) -> None:
            calls.append("echo.aclose")

    session = Session(
        _full_config(
            stt=CloseSTT(),
            tts=AsyncCloseTTS(),
            vad=AsyncCloseVAD(),
            noise_reducer=CloseNoiseReducer(),
            echo_canceller=AsyncCloseEchoCanceller(),
        )
    )

    await getattr(session, method_name)()

    assert calls == [
        "stt.close",
        "tts.aclose",
        "vad.close",
        "noise.close",
        "echo.aclose",
    ]


@pytest.mark.asyncio
async def test_shutdown_ends_active_stt_stream_without_close_hook():
    class EndOnlySTT(FakeSTT):
        def __init__(self) -> None:
            super().__init__()
            self.end_calls = 0

        async def end_stream(self) -> None:
            self.end_calls += 1
            await self._queue.put(None)

    stt = EndOnlySTT()
    session = Session(_full_config(stt=stt))
    session._stt_active = True

    await session.shutdown()

    assert stt.end_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["stop", "shutdown"])
async def test_external_outbound_queue_survives_session_teardown(method_name: str):
    transport = FakeTransport()
    queue = BoundedAudioQueue()
    session = Session(_full_config(transport=transport, outbound_queue=queue))

    await session.start()
    await getattr(session, method_name)()

    assert await queue.put(_make_chunk())
    assert queue.qsize() == 1


@pytest.mark.asyncio
async def test_replay_gated_audio_stays_bot_speaking_until_outbound_drain():
    transport = FakeTransport()
    session = Session(_full_config(transport=transport))
    events = [
        TTSAudio(chunk=_make_chunk()),
        TTSAudio(chunk=_make_chunk()),
    ]

    await session.replay_gated_audio(events)

    assert session._turn_manager.state == TurnManagerState.BOT_SPEAKING
    assert session._outbound_queue.qsize() == 2

    await session._audio_router._drain_outbound_audio()

    assert session._turn_manager.state == TurnManagerState.IDLE
    assert len(transport.sent) == 2


@pytest.mark.asyncio
async def test_session_start_idempotent():
    session = Session(_full_config())
    await session.start()
    await session.start()
    assert session.is_running
    await session.stop()


@pytest.mark.asyncio
async def test_session_start_rolls_back_after_connect_failure():
    class FlakyTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.connect_calls = 0

        async def connect(self) -> None:
            self.connect_calls += 1
            if self.connect_calls == 1:
                raise RuntimeError("boom")
            await super().connect()

    transport = FlakyTransport()
    session = Session(_full_config(transport=transport))

    with pytest.raises(RuntimeError, match="boom"):
        await session.start()

    assert not session.is_running
    assert session._audio_router.pipeline_task is None
    assert session._audio_router.outbound_task is None

    await session.start()

    assert session.is_running
    assert transport.connect_calls == 2
    assert transport.connected

    await session.stop()


@pytest.mark.asyncio
async def test_session_stop_idempotent():
    session = Session(_full_config())
    await session.stop()
    assert not session.is_running


@pytest.mark.asyncio
async def test_stop_keeps_sqlite_journal_and_bundle_readable(tmp_path):
    session_id = "sess"
    transport = FakeTransport()
    journal = SqliteJournal(session_id, data_dir=tmp_path)
    artifact_store = FilesystemArtifactStore(session_id, data_dir=tmp_path)
    ref = artifact_store.put(b"artifact-bytes")
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="before_stop",
        session_id=session_id,
        input_ref=ref,
    )
    session = Session(
        _full_config(
            transport=transport,
            journal=journal,
            artifact_store=artifact_store,
            session_id=session_id,
        )
    )

    await session.stop()

    assert (
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="after_stop",
            session_id=session_id,
        )
        == -1
    )

    assert session.journal is not None
    records = session.journal.read()
    assert [record.name for record in records] == ["before_stop"]

    bundle_path = tmp_path / "after-stop-full.zip"
    session.export_debug_bundle(str(bundle_path))
    with zipfile.ZipFile(bundle_path) as zf:
        assert "journal.ndjson" in zf.namelist()
        assert f"artifacts/{ref}.bin" in zf.namelist()


@pytest.mark.asyncio
async def test_stop_keeps_in_memory_bundle_exportable(tmp_path):
    session_id = "sess"
    transport = FakeTransport()
    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(artifact_store=artifact_store)
    ref = artifact_store.put(b"artifact-bytes")
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="before_stop",
        session_id=session_id,
        input_ref=ref,
    )
    session = Session(
        _full_config(
            transport=transport,
            journal=journal,
            artifact_store=artifact_store,
            session_id=session_id,
        )
    )

    await session.stop()

    assert artifact_store.get(ref) is None
    assert session.journal is not None
    assert [record.name for record in session.journal.read()] == ["before_stop"]

    bundle_path = tmp_path / "after-stop-light.zip"
    session.export_debug_bundle(str(bundle_path))
    with zipfile.ZipFile(bundle_path) as zf:
        assert f"artifacts/{ref}.bin" in zf.namelist()


# ── Cancellation tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_turn_resets_state():
    session = Session(_full_config())
    session._turn_state = TurnState.LISTENING
    turn = TurnContext("test-turn", CancelToken())
    session._turn = turn
    await session.cancel_turn()
    assert session.turn_state == TurnState.IDLE
    assert turn.cancel_token.is_cancelled


@pytest.mark.asyncio
async def test_cancel_turn_barge_in_emits_interruption():
    session = Session(_full_config())
    session._turn_state = TurnState.BOT_SPEAKING
    session._turn = TurnContext("test-turn", CancelToken())

    received: list = []
    session.event_bus.subscribe(Interruption, lambda e: received.append(e))

    await session.cancel_turn(barge_in=True)
    assert len(received) == 1
    assert session.turn_state == TurnState.IDLE


@pytest.mark.asyncio
async def test_journaled_task_records_scheduled_and_completed():
    """``RuntimeScope.create_journaled_task`` must write ``task_scheduled`` at creation and
    ``task_completed`` when the coroutine finishes cleanly."""
    journal = InMemoryRingBuffer(capacity=32)
    session = Session(_full_config(journal=journal))
    session._turn = TurnContext("tj-1", CancelToken())

    async def _ok() -> str:
        return "ok"

    task = session._runtime_scope.create_journaled_task(
        _ok(), name="unit_test_task", journal_sink=session._journal_sink
    )
    await task
    # add_done_callback schedules the emit callback — let it run.
    await asyncio.sleep(0)

    names = [r.name for r in journal.read()]
    assert "task_scheduled" in names
    assert "task_completed" in names
    scheduled = next(r for r in journal.read() if r.name == "task_scheduled")
    completed = next(r for r in journal.read() if r.name == "task_completed")
    assert scheduled.data["task_name"] == "unit_test_task"
    assert completed.data["task_name"] == "unit_test_task"


@pytest.mark.asyncio
async def test_journaled_task_records_cancelled():
    journal = InMemoryRingBuffer(capacity=32)
    session = Session(_full_config(journal=journal))

    async def _slow() -> None:
        await asyncio.sleep(10.0)

    task = session._runtime_scope.create_journaled_task(
        _slow(), name="slow_task", journal_sink=session._journal_sink
    )
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0)

    names = [r.name for r in journal.read()]
    assert "task_cancelled" in names


@pytest.mark.asyncio
async def test_journaled_task_records_raised():
    journal = InMemoryRingBuffer(capacity=32)
    session = Session(_full_config(journal=journal))

    async def _boom() -> None:
        raise ValueError("explosion")

    task = session._runtime_scope.create_journaled_task(
        _boom(), name="boom_task", journal_sink=session._journal_sink
    )
    try:
        await task
    except ValueError:
        pass
    await asyncio.sleep(0)

    recs = journal.read()
    raised = [r for r in recs if r.name == "task_raised"]
    assert len(raised) == 1
    assert raised[0].data["exc_type"] == "ValueError"


@pytest.mark.asyncio
async def test_turn_state_changed_recorded_on_transition():
    """Every TurnManager state change must land as a journal record —
    no more "why did it go to PROCESSING" bugs that require a logger
    dump to answer.

    Drive the transition directly via start_turn() / end_turn() so the
    test doesn't depend on VAD timing.
    """
    journal = InMemoryRingBuffer(capacity=64)
    session = Session(_full_config(journal=journal))
    await session._turn_manager.start_turn()
    await session._turn_manager.end_turn()

    transitions = [r for r in journal.read() if r.name == "turn_state_changed"]
    assert transitions, "expected at least one turn_state_changed record"
    reasons = {r.data["reason"] for r in transitions}
    assert "manual_start" in reasons
    assert "manual_end" in reasons
    # Idle → UserSpeaking then UserSpeaking → Processing.
    pairs = {(r.data["from"], r.data["to"]) for r in transitions}
    assert ("idle", "user_speaking") in pairs
    assert ("user_speaking", "processing") in pairs


@pytest.mark.asyncio
async def test_audio_queue_drop_recorded_when_queue_overflows():
    """BoundedAudioQueue drops must land in the journal via the
    ``on_drop`` hook so backpressure is visible from a bundle."""
    journal = InMemoryRingBuffer(capacity=32)
    session = Session(_full_config(journal=journal))
    # Shrink the outbound queue so we can overflow it deterministically.
    q = session._outbound_queue
    q._max_size = 2  # type: ignore[attr-defined]

    chunk = _make_chunk(n_bytes=320)
    await q.put(chunk)
    await q.put(chunk)
    # This one should be dropped (DROP_OLDEST policy).
    await q.put(chunk)

    drops = [r for r in journal.read() if r.name == "audio_queue_drop"]
    assert len(drops) == 1
    assert drops[0].data["queue"] == "outbound_audio"
    assert drops[0].data["kind"] == "drop_oldest"
    assert drops[0].data["total_drops"] == 1


@pytest.mark.asyncio
async def test_pipeline_heartbeat_emits_records_at_interval():
    """Drive ``_emit_heartbeats`` directly with a short interval and
    verify each record carries the expected shape (loop_lag_ms,
    queue len, drops)."""
    journal = InMemoryRingBuffer(capacity=64)
    session = Session(_full_config(journal=journal))
    session._is_running = True

    task = asyncio.create_task(session._emit_heartbeats(interval_s=0.05))
    try:
        await asyncio.sleep(0.25)
    finally:
        session._is_running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    heartbeats = [r for r in journal.read() if r.name == "pipeline_heartbeat"]
    assert len(heartbeats) >= 2, f"expected at least 2 heartbeats, got {len(heartbeats)}"
    data = heartbeats[0].data
    assert data["interval_ms"] == 50
    assert "loop_lag_ms" in data
    assert "outbound_queue_len" in data
    assert "outbound_queue_drops" in data


@pytest.mark.asyncio
async def test_schedule_turn_ended_cancels_inflight_stt_commit():
    """Regression test for plan-7 flakiness.

    When VADStopSpeaking fires, ``STTCommitter.schedule`` creates
    a task that calls ``stt.commit_segment``.  If SmartTurn immediately
    declares the turn complete, ``TurnEnded`` fires before the commit
    task has a chance to cancel — and previously ``_schedule_turn_ended``
    only cancelled the *scheduled* task, not the *in-flight* one.  That
    left ``commit_segment`` racing with ``_handle_end_of_speech``'s
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

    events = []

    class _RaceSTT:
        async def start_stream(self) -> None: ...
        async def send_audio(self, chunk) -> None: ...

        async def commit_segment(self) -> bool:
            events.append("commit")
            await asyncio.sleep(0.05)
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
    await asyncio.sleep(0.001)
    session._schedule_turn_ended(TurnEnded(turn_id="race-turn"))
    for _ in range(20):
        await asyncio.sleep(0.01)

    # Invariant: we never observe BOTH commit_done AND end_stream in
    # the same run — the in-flight cancel closes the window.
    assert not ("commit_done" in events and "end_stream" in events), (
        f"in-flight commit was not cancelled on TurnEnded: events={events}"
    )


@pytest.mark.asyncio
async def test_cancel_turn_barge_in_propagates_signal_through_all_stages():
    """WS3 T3.8: a barge-in must dispatch an InterruptSignal through
    every stage, producing one ControlSignalRecord per stage in the
    journal so replay can see who observed the signal and when.
    """
    journal = InMemoryRingBuffer(capacity=64)
    session = Session(_full_config(journal=journal))
    session._turn_state = TurnState.BOT_SPEAKING
    session._turn = TurnContext("test-turn-signal", CancelToken())

    await session.cancel_turn(barge_in=True)

    signal_records = [r for r in journal.read() if r.kind == JournalRecordKind.CONTROL]
    # One per stage plus the trailing cause record.
    stage_records = [r for r in signal_records if r.name == "control_signal"]
    cause_records = [r for r in signal_records if r.name == "control_signal_cause"]
    observed = {r.data["observed_stage"] for r in stage_records}
    # Telephony doesn't have its own stage; the session only fans the
    # signal through helpers when at least one is registered.
    assert observed == {
        "transport",
        "tts",
        "agent",
        "turn",
        "stt",
        "vad",
        "audio",
    }
    # Every stage record carries the same signal_id so a replay UI can
    # group the upstream walk into one logical event.
    signal_ids = {r.data["signal_id"] for r in stage_records}
    assert len(signal_ids) == 1
    # The cause record links the signal back to "barge_in".
    assert len(cause_records) == 1
    assert cause_records[0].data["cause"] == "barge_in"
    assert cause_records[0].data["signal_id"] == next(iter(signal_ids))


@pytest.mark.asyncio
async def test_cancel_tts_playback_resets_state():
    session = Session(_full_config())
    session._turn_state = TurnState.BOT_SPEAKING
    turn = TurnContext("test-turn", CancelToken())
    session._turn = turn
    await session.cancel_tts_playback()
    assert session.turn_state == TurnState.IDLE
    # cancel_tts_playback should NOT cancel the shared token —
    # only TTS is stopped, agent streams can continue.
    assert not turn.cancel_token.is_cancelled


@pytest.mark.asyncio
async def test_reset_state():
    session = Session(_full_config())
    session._turn_state = TurnState.PROCESSING
    turn = TurnContext("test-turn", CancelToken())
    session._turn = turn
    await session.reset_state()
    assert session.turn_state == TurnState.IDLE
    assert turn.cancel_token.is_cancelled


# ── Pipeline orchestration tests ───────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_emits_audio_in_events():
    chunks = [_make_chunk(), _make_chunk()]
    transport = FakeTransport(chunks=chunks)
    config = _full_config(transport=transport, enable_vad=False)
    session = Session(config)

    received: list[AudioIn] = []
    session.event_bus.subscribe(AudioIn, lambda e: received.append(e))

    await session.start()
    await asyncio.sleep(0.05)
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
    for event_type in (TurnStarted, STTFinal, TurnEnded, AgentFinal):
        session.event_bus.subscribe(event_type, lambda e: events_received.append(e))

    await session.start()
    await asyncio.sleep(0.2)

    type_names = [type(event).__name__ for event in events_received]
    assert type_names.count("TurnStarted") == 1
    assert "STTFinal" in type_names
    assert "TurnEnded" in type_names
    assert "AgentFinal" in type_names
    assert stt.start_count == 1
    assert stt.end_count == 1
    assert stt.sent_chunks == chunks

    await session.stop()


@pytest.mark.asyncio
async def test_pipeline_noise_reduction():
    chunk = _make_chunk()
    transport = FakeTransport(chunks=[chunk])

    class TrackingNoiseReducer:
        def __init__(self) -> None:
            self.processed = False

        async def process(self, c: AudioChunk) -> AudioChunk:
            self.processed = True
            return c

    nr = TrackingNoiseReducer()
    config = _full_config(
        transport=transport, noise_reducer=nr, enable_vad=False, enable_noise_reduction=True
    )
    session = Session(config)

    await session.start()
    await asyncio.sleep(0.05)
    await session.stop()

    assert nr.processed


@pytest.mark.asyncio
async def test_handle_end_of_speech_clears_turn_id_on_stt_timeout():
    session = Session(_full_config())
    session._turn = TurnContext("turn-stale", CancelToken())
    session._timeout_config.stt_timeout = 0.01
    session._turn.stt_final_future = asyncio.get_running_loop().create_future()

    await session._handle_end_of_speech()

    assert session._turn is None
    assert session.turn_state == TurnState.IDLE


@pytest.mark.asyncio
async def test_handle_end_of_speech_clears_turn_id_on_empty_transcript():
    session = Session(_full_config())
    session._turn = TurnContext("turn-stale", CancelToken())
    done = asyncio.get_running_loop().create_future()
    done.set_result("")
    session._turn.stt_final_future = done

    await session._handle_end_of_speech()

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

    session._stt_committer.schedule(VADStopSpeaking(), turn=session._turn)
    await asyncio.sleep(0.05)

    assert stt.commit_calls == 1
    assert session._turn is not None
    assert session._turn_manager.state == TurnManagerState.USER_PAUSED
    assert session._turn.transcript_text == "hello"


@pytest.mark.asyncio
async def test_handle_end_of_speech_no_duplicate_stt_final():
    """_handle_end_of_speech must not re-emit per-segment STTFinals."""
    session = Session(_full_config())
    session._turn = TurnContext("turn-stale", CancelToken())
    session._turn.append_stt_segment("hello")
    session._turn.append_stt_segment("world")

    timeline: list[Event] = []
    session.event_bus.subscribe(STTFinal, lambda e: timeline.append(e))
    session.event_bus.subscribe(AgentFinal, lambda e: timeline.append(e))

    await session._handle_end_of_speech()

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

    await session._run_streaming_agent("call me at 415-555-2671", token=None)

    assert session.turn_state == TurnState.IDLE
    assert any(isinstance(e.exception, AgentTimeoutError) for e in errors)


@pytest.mark.asyncio
async def test_streaming_agent_strip_markdown_writes_journal_record():
    class MarkdownAgent:
        async def run(self, text: str) -> str:
            return "Go to **Settings** first."

    journal = InMemoryRingBuffer()
    session = Session(_full_config(agent=MarkdownAgent(), journal=journal, strip_markdown=True))
    session._turn = TurnContext("turn-markdown", CancelToken())

    await session._run_streaming_agent("help", token=None)

    records = [record for record in journal.read() if record.name == "markdown_stripped"]
    assert records, "expected a markdown_stripped record"
    final_record = next(r for r in records if r.data.get("phase") == "streaming_final")
    assert final_record.turn_id == "turn-markdown"
    assert final_record.data == {
        "phase": "streaming_final",
        "changed": True,
        "original_text": "Go to **Settings** first.",
        "stripped_text": "Go to Settings first.",
    }


@pytest.mark.asyncio
async def test_prepare_tts_payload_writes_journal_record():
    class PrefixProcessor:
        def process(self, payload: TTSInput, *, is_final: bool, is_streaming: bool) -> TTSInput:
            return TTSInput(text=f"speak: {payload.text}", format=payload.format)

    journal = InMemoryRingBuffer()
    session = Session(
        _full_config(
            journal=journal,
            output_processors=[PrefixProcessor()],
        )
    )
    session._turn = TurnContext("turn-tts-prepared", CancelToken())

    payload = session._tts_scheduler.prepare("hello", is_streaming=False, is_final=True)

    assert payload.text == "speak: hello"
    records = [record for record in journal.read() if record.name == "tts_payload_prepared"]
    assert len(records) == 1
    assert records[0].turn_id == "turn-tts-prepared"
    assert records[0].data == {
        "is_streaming": False,
        "is_final": True,
        "changed": True,
        "original_text": "hello",
        "original_format": "plain",
        "prepared_text": "speak: hello",
        "prepared_format": "plain",
        "processors": ["PrefixProcessor"],
        "ssml_downgraded": False,
    }


@pytest.mark.asyncio
async def test_pause_commit_journals_segment_commit_and_final():
    stt = SegmentingSTT(["hello"])
    journal = InMemoryRingBuffer()
    session = Session(
        _full_config(
            stt=stt,
            journal=journal,
            turn_manager_config=TurnManagerConfig(
                end_of_turn_silence_ms=1000,
                stt_segment_silence_ms=1,
            ),
        )
    )
    session._turn = TurnContext("turn-segment-journal", CancelToken())
    session._turn.stt_has_uncommitted_audio = True
    session._stt_committer.mark_active()
    session._turn_manager._state = TurnManagerState.USER_PAUSED
    session._stt_committer.start_event_loop(session._turn)

    await session._stt_committer._start_segment_commit(turn=session._turn)
    await asyncio.sleep(0.05)

    records = [record for record in journal.read() if record.name.startswith("stt_segment_")]
    records_by_name = {record.name: record for record in records}
    assert set(records_by_name) == {
        "stt_segment_commit_requested",
        "stt_segment_final",
        "stt_segment_commit_result",
    }
    assert records_by_name["stt_segment_commit_requested"].data == {
        "segment_index": 1,
        "transcript_text": "",
        "pending_commit_bytes": None,
    }
    assert records_by_name["stt_segment_final"].data == {
        "segment_index": 1,
        "text": "hello",
        "track": None,
        "transcript_text": "hello",
    }
    assert records_by_name["stt_segment_commit_result"].data == {
        "segment_index": 1,
        "committed": True,
        "transcript_text": "",
    }


@pytest.mark.asyncio
async def test_shutdown_cancels_runtime_scoped_stt_pause_commit() -> None:
    journal = InMemoryRingBuffer(capacity=64)
    session = Session(
        _full_config(
            journal=journal,
            turn_manager_config=TurnManagerConfig(
                end_of_turn_silence_ms=1000,
                stt_segment_silence_ms=1000,
            ),
        )
    )
    session._is_running = True
    session._turn = TurnContext("turn-runtime-scope", CancelToken())
    session._turn.stt_has_uncommitted_audio = True
    session._stt_committer.mark_active()
    session._turn_manager._state = TurnManagerState.USER_PAUSED

    session._stt_committer.schedule(VADStopSpeaking(), turn=session._turn)
    task = session._stt_committer._pause_commit_task
    assert task is not None
    assert session._runtime_scope.tasks("stt_pause_commit") == (task,)

    await session.shutdown()

    records = [
        record for record in journal.read() if record.data.get("task_name") == "stt_pause_commit"
    ]
    assert task.cancelled()
    assert session._stt_committer._pause_commit_task is None
    assert session._runtime_scope.empty
    assert [record.name for record in records] == ["task_scheduled", "task_cancelled"]


@pytest.mark.asyncio
async def test_stop_cancels_runtime_scoped_stt_pause_commit() -> None:
    journal = InMemoryRingBuffer(capacity=64)
    session = Session(
        _full_config(
            journal=journal,
            turn_manager_config=TurnManagerConfig(
                end_of_turn_silence_ms=1000,
                stt_segment_silence_ms=1000,
            ),
        )
    )
    session._is_running = True
    session._turn = TurnContext("turn-runtime-scope", CancelToken())
    session._turn.stt_has_uncommitted_audio = True
    session._stt_committer.mark_active()
    session._turn_manager._state = TurnManagerState.USER_PAUSED

    session._stt_committer.schedule(VADStopSpeaking(), turn=session._turn)
    task = session._stt_committer._pause_commit_task
    assert task is not None

    await session.stop()

    records = [
        record for record in journal.read() if record.data.get("task_name") == "stt_pause_commit"
    ]
    assert task.cancelled()
    assert session._stt_committer._pause_commit_task is None
    assert session._runtime_scope.empty
    assert [record.name for record in records] == ["task_scheduled", "task_cancelled"]


@pytest.mark.asyncio
async def test_tts_audio_and_markers_are_journaled_with_artifact_ref():
    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(artifact_store=artifact_store)
    session = Session(
        _full_config(
            tts=MarkerTTS(),
            journal=journal,
            artifact_store=artifact_store,
        )
    )
    session._turn = TurnContext("turn-tts-audio", CancelToken())

    await session._tts_scheduler.synthesize("hello", token=None)

    audio_records = [record for record in journal.read() if record.name == "tts_audio"]
    marker_records = [record for record in journal.read() if record.name == "tts_markers"]
    tts_frame_records = [record for record in journal.read() if record.name == "tts_frame"]

    assert len(audio_records) == 1
    assert audio_records[0].turn_id == "turn-tts-audio"
    # Session-level tts_audio record no longer carries output_ref — WS3
    # T3.9 moved artifact capture into TTSStage, which emits one
    # ``tts_frame`` record per chunk with ``output_ref`` set.
    assert audio_records[0].data == {
        "audio_bytes": 320,
        "duration_ms": 10.0,
        "sample_rate": 16000,
        "channels": 1,
        "sample_width": 2,
        "encoding": "pcm",
        "bypass_gate": False,
    }
    assert len(tts_frame_records) >= 1
    assert tts_frame_records[0].turn_id == "turn-tts-audio"
    assert tts_frame_records[0].output_ref is not None
    assert artifact_store.has(tts_frame_records[0].output_ref)
    assert len(marker_records) == 1
    assert marker_records[0].data == {"markers": [{"word": "hello", "start_ms": 0}]}


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
        session.event_bus.subscribe(et, lambda e: events_received.append(e))

    await session.start()
    await asyncio.sleep(0.2)
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
async def test_session_event_bus_accessible():
    session = Session(_full_config())
    assert session.event_bus is not None
    received: list = []
    session.event_bus.subscribe(STTFinal, lambda e: received.append(e))
    await session.event_bus.emit(STTFinal(text="test"))
    assert len(received) == 1


@pytest.mark.asyncio
async def test_session_subscribe_agent_events_helper():
    session = Session(_full_config())

    deltas: list[str] = []
    finals: list[str] = []
    tools_started: list[str] = []
    tools_results: list[str] = []

    registrations = session.subscribe_agent_events(
        on_delta=lambda e: deltas.append(e.text),
        on_final=lambda e: finals.append(e.text),
        on_tool_started=lambda e: tools_started.append(e.tool_name),
        on_tool_result=lambda e: tools_results.append(e.result),
    )

    await session.event_bus.emit(AgentDelta(text="chunk"))
    await session.event_bus.emit(AgentFinal(text="done"))
    await session.event_bus.emit(ToolCallStarted(tool_name="lookup", call_id="c1"))
    await session.event_bus.emit(ToolCallResult(call_id="c1", result="42"))

    assert deltas == ["chunk"]
    assert finals == ["done"]
    assert tools_started == ["lookup"]
    assert tools_results == ["42"]

    session.unsubscribe_handlers(registrations)
    await session.event_bus.emit(AgentFinal(text="done again"))
    assert deltas == ["chunk"]
    assert finals == ["done"]


@pytest.mark.asyncio
async def test_session_on_convenience_method():
    """session.on() subscribes with unwrapped callback arguments."""
    session = Session(_full_config())

    transcripts: list[str] = []
    responses: list[str] = []
    deltas: list[str] = []
    tools: list[tuple[str, str]] = []
    tool_results: list[tuple[str, str]] = []
    lifecycle: list[str] = []
    errors: list[tuple[BaseException, str]] = []

    registrations = session.on(
        user_transcript=lambda text: transcripts.append(text),
        agent_response=lambda text: responses.append(text),
        agent_delta=lambda text: deltas.append(text),
        tool_started=lambda name, cid: tools.append((name, cid)),
        tool_result=lambda cid, result: tool_results.append((cid, result)),
        turn_started=lambda: lifecycle.append("turn_started"),
        turn_ended=lambda: lifecycle.append("turn_ended"),
        bot_started_speaking=lambda: lifecycle.append("bot_started"),
        bot_stopped_speaking=lambda: lifecycle.append("bot_stopped"),
        interruption=lambda: lifecycle.append("interruption"),
        error=lambda exc, ctx: errors.append((exc, ctx)),
    )

    # Emit events and verify callbacks receive unwrapped args.
    await session.event_bus.emit(STTFinal(text="hello"))
    await session.event_bus.emit(AgentDelta(text="hi "))
    await session.event_bus.emit(AgentFinal(text="hi there"))
    await session.event_bus.emit(ToolCallStarted(tool_name="search", call_id="c1"))
    await session.event_bus.emit(ToolCallResult(call_id="c1", result="found"))
    await session.event_bus.emit(TurnStarted())
    await session.event_bus.emit(TurnEnded())
    await session.event_bus.emit(BotStartedSpeaking())
    await session.event_bus.emit(BotStoppedSpeaking())
    await session.event_bus.emit(Interruption())
    await session.event_bus.emit(Error(exception=ValueError("boom"), stage=ErrorStage.AGENT))

    assert transcripts == ["hello"]
    assert responses == ["hi there"]
    assert deltas == ["hi "]
    assert tools == [("search", "c1")]
    assert tool_results == [("c1", "found")]
    assert lifecycle == [
        "turn_started",
        "turn_ended",
        "bot_started",
        "bot_stopped",
        "interruption",
    ]
    assert len(errors) == 1
    assert str(errors[0][0]) == "boom"
    assert errors[0][1] == "agent"

    # Unsubscribe and verify no further callbacks.
    session.unsubscribe_handlers(registrations)
    await session.event_bus.emit(STTFinal(text="ignored"))
    assert transcripts == ["hello"]


@pytest.mark.asyncio
async def test_session_events_include_correlation_ids():
    session = Session(_full_config())
    seen: list[TurnStarted | Interruption] = []
    session.event_bus.subscribe(TurnStarted, lambda e: seen.append(e))
    session.event_bus.subscribe(Interruption, lambda e: seen.append(e))

    await session._emit(TurnStarted())
    await session.cancel_turn(barge_in=True)

    assert seen
    for event in seen:
        assert event.session_id == session.session_id


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

    await session.start()
    await asyncio.sleep(0.2)
    await session.stop()

    assert session.turn_state == TurnState.IDLE


@pytest.mark.asyncio
async def test_playback_mark_ack_scoped_to_current_turn():
    """Playback marks are scoped to the current TurnContext.
    Each new turn has its own playback_mark_to_bytes map, so marks
    from a previous turn are naturally absent from the current turn's map."""
    transport = FakePlaybackAckTransport()
    session = Session(_full_config(transport=transport))
    # Use a small interval so a single test chunk triggers a mark.
    session._playback_mark_bytes_interval = 1

    # ── First turn ──
    session._turn = TurnContext("turn-first", CancelToken())
    await session._outbound_queue.put(_make_chunk())
    await session._audio_router._drain_outbound_audio()
    first_turn_marks = list(session._turn.playback_mark_to_bytes.keys())
    assert len(first_turn_marks) == 1

    # ── Second turn (replaces the TurnContext) ──
    session._is_running = True
    with patch.object(session._stt_committer, "start_event_loop"):
        await session._on_turn_started(TurnStarted())
    session._is_running = False

    await session._outbound_queue.put(_make_chunk())
    await session._audio_router._drain_outbound_audio()
    second_turn_marks = list(session._turn.playback_mark_to_bytes.keys())
    assert len(second_turn_marks) == 1

    # Ack for the second turn's mark works.
    session._audio_router.on_playback_ack(PlaybackMarkAck(mark_name=second_turn_marks[0]))
    assert len(session._turn.playback_ack_log) == 1
    assert session._turn.playback_ack_log[0][1] == 320


@pytest.mark.asyncio
async def test_playback_mark_ack_tracks_transport_confirmed_name():
    class CanonicalizingPlaybackAckTransport(FakePlaybackAckTransport):
        async def send_playback_mark(self, name: str | None = None) -> str:
            requested_name = name or f"mark_{len(self.playback_marks) + 1}"
            canonical_name = f"canonical::{requested_name}"
            self.playback_marks.append(canonical_name)
            return canonical_name

    transport = CanonicalizingPlaybackAckTransport()
    session = Session(_full_config(transport=transport))
    session._playback_mark_bytes_interval = 1
    session._turn = TurnContext("test-turn", CancelToken())

    await session._outbound_queue.put(_make_chunk())
    await session._audio_router._drain_outbound_audio()

    canonical_mark = transport.playback_marks[-1]
    session._audio_router.on_playback_ack(PlaybackMarkAck(mark_name=canonical_mark))

    assert len(session._turn.playback_ack_log) == 1
    assert session._turn.playback_ack_log[0][1] == 320


@pytest.mark.asyncio
async def test_trailing_playback_mark_emitted_while_session_running():
    transport = FakePlaybackAckTransport()
    session = Session(_full_config(transport=transport))
    session._playback_mark_bytes_interval = 10_000

    await session.start()
    session._turn = TurnContext("test-turn", CancelToken())
    await session._outbound_queue.put(_make_chunk())

    for _ in range(20):
        if transport.playback_marks:
            break
        await asyncio.sleep(0.01)

    assert len(transport.playback_marks) == 1
    await session.stop()


@pytest.mark.asyncio
async def test_buffered_transport_delivery_is_counted_only_after_report() -> None:
    transport = ReportingTransport()
    session = Session(_full_config(transport=transport))
    session._turn = TurnContext("test-turn", CancelToken())
    seen: list[AudioOut] = []
    session.event_bus.subscribe(AudioOut, lambda event: seen.append(event))

    chunk = _make_chunk()
    await session._outbound_queue.put(chunk)
    await session._audio_router._drain_outbound_audio()

    assert transport.sent == [chunk]
    assert session._turn.audio_bytes_sent == 0
    assert seen == []

    await session.event_bus.emit(
        TransportAudioDelivered(
            chunk=chunk,
            turn_id=session._turn.id,
            turn_ref=session._turn,
        )
    )

    assert session._turn.audio_bytes_sent == len(chunk.data)
    assert len(seen) == 1
    assert seen[0].chunk is chunk
    assert seen[0].turn_id == "test-turn"


@pytest.mark.asyncio
async def test_failed_send_does_not_emit_audio_out_or_count_bytes() -> None:
    class RejectingTransport(FakePlaybackAckTransport):
        async def send_audio(self, chunk: AudioChunk) -> bool:
            return False

    transport = RejectingTransport()
    session = Session(_full_config(transport=transport))
    session._playback_mark_bytes_interval = 1
    session._turn = TurnContext("test-turn", CancelToken())
    seen: list[AudioOut] = []
    session.event_bus.subscribe(AudioOut, lambda event: seen.append(event))

    await session._outbound_queue.put(_make_chunk())
    await session._audio_router._drain_outbound_audio()

    assert session._turn.audio_bytes_sent == 0
    assert session._turn.bytes_since_last_mark == 0
    assert transport.playback_marks == []
    assert seen == []


@pytest.mark.asyncio
async def test_session_applies_output_processors_before_tts() -> None:
    class CaptureTTS(FakeTTS):
        def __init__(self) -> None:
            self.payloads: list[TTSInput] = []

        @property
        def supports_ssml(self) -> bool:
            return True

        async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
            self.payloads.append(payload)
            yield TTSEvent(type=TTSEventType.AUDIO, audio=_make_chunk())

    class PrefixProcessor:
        def process(self, payload: TTSInput, *, is_final: bool, is_streaming: bool) -> TTSInput:
            return TTSInput(text=f"speak: {payload.text}", format=payload.format)

    tts = CaptureTTS()
    session = Session(
        _full_config(
            tts=tts,
            output_processors=[PrefixProcessor()],
            transport=FakeTransport(chunks=[_make_chunk(), _make_chunk()]),
            stt=FakeSTT(transcript="hello"),
        )
    )

    session._turn = TurnContext("turn-output-proc", CancelToken())
    await session._run_streaming_agent("call me at 415-555-2671", token=None)

    assert tts.payloads
    assert tts.payloads[0].text.startswith("speak: ")
    assert tts.payloads[0].format == "plain"


@pytest.mark.asyncio
async def test_session_falls_back_to_plain_when_ssml_not_supported() -> None:
    class CaptureTTS(FakeTTS):
        def __init__(self) -> None:
            self.payloads: list[TTSInput] = []

        @property
        def supports_ssml(self) -> bool:
            return False

        async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
            self.payloads.append(payload)
            yield TTSEvent(type=TTSEventType.AUDIO, audio=_make_chunk())

    tts = CaptureTTS()
    session = Session(
        _full_config(
            tts=tts,
            output_processors=[
                PauseProcessor(
                    pattern=r"\+?\d[\d\s().-]{5,}\d",
                    unit_pattern=r"\d",
                    minimum_units=7,
                )
            ],
            transport=FakeTransport(chunks=[_make_chunk(), _make_chunk()]),
            stt=FakeSTT(transcript="call AT&T at 415-555-2671"),
        )
    )

    session._turn = TurnContext("turn-ssml-fallback", CancelToken())
    await session._run_streaming_agent("call AT&T at 415-555-2671", token=None)

    assert tts.payloads
    assert tts.payloads[0].format == "plain"
    assert "<break" not in tts.payloads[0].text
    assert "AT&T" in tts.payloads[0].text
    assert "AT&amp;T" not in tts.payloads[0].text
    assert "4 1 5" in tts.payloads[0].text


@pytest.mark.asyncio
async def test_session_falls_back_to_plain_unescapes_ssml_entities() -> None:
    class CaptureTTS(FakeTTS):
        def __init__(self) -> None:
            self.payloads: list[TTSInput] = []

        @property
        def supports_ssml(self) -> bool:
            return False

        async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
            self.payloads.append(payload)
            yield TTSEvent(type=TTSEventType.AUDIO, audio=_make_chunk())

    tts = CaptureTTS()
    session = Session(
        _full_config(
            tts=tts,
            output_processors=[
                PauseProcessor(
                    pattern=r"\+?\d[\d\s().-]{5,}\d",
                    unit_pattern=r"\d",
                    minimum_units=7,
                )
            ],
        )
    )

    session._turn = TurnContext("turn-ssml-unescape", CancelToken())
    await session._run_streaming_agent("Call AT&T at 415-555-2671", token=None)

    assert tts.payloads
    assert tts.payloads[0].format == "plain"
    assert "AT&T" in tts.payloads[0].text
    assert "AT&amp;T" not in tts.payloads[0].text


@pytest.mark.asyncio
async def test_session_composes_phonetic_and_phone_processors() -> None:
    class CaptureTTS(FakeTTS):
        def __init__(self) -> None:
            self.payloads: list[TTSInput] = []

        @property
        def supports_ssml(self) -> bool:
            return False

        async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
            if isinstance(payload, str):
                payload = TTSInput(payload)
            self.payloads.append(payload)
            yield TTSEvent(type=TTSEventType.AUDIO, audio=_make_chunk())

    tts = CaptureTTS()
    session = Session(
        _full_config(
            tts=tts,
            output_processors=[
                PhoneticReplacementProcessor({"Siobhan": "shi-vawn"}),
                PauseProcessor(
                    pattern=r"\+?\d[\d\s().-]{5,}\d",
                    unit_pattern=r"\d",
                    minimum_units=7,
                    pause_ms=140,
                ),
            ],
        )
    )

    session._turn = TurnContext("turn-phonetic", CancelToken())
    await session._run_streaming_agent("call Siobhan at 415-555-2671", token=None)

    assert tts.payloads
    # provider doesn't support SSML, so we should receive plain text fallback.
    assert tts.payloads[0].format == "plain"
    assert "shi-vawn" in tts.payloads[0].text
    assert "4 1 5" in tts.payloads[0].text
