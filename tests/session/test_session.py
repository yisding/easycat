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

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent.append(chunk)

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

    assert session._output_processors == [processor]


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

    await session._drain_outbound_audio()

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
    assert session._pipeline_task is None
    assert session._outbound_task is None

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

    await session._run_pipeline()

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

    await session._run_pipeline()

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
    session._stt_final_future = asyncio.get_running_loop().create_future()

    await session._handle_end_of_speech()

    assert session._turn is None
    assert session.turn_state == TurnState.IDLE


@pytest.mark.asyncio
async def test_handle_end_of_speech_clears_turn_id_on_empty_transcript():
    session = Session(_full_config())
    session._turn = TurnContext("turn-stale", CancelToken())
    done = asyncio.get_running_loop().create_future()
    done.set_result("")
    session._stt_final_future = done

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
    session._stt_active = True
    session._turn_manager._state = TurnManagerState.USER_PAUSED
    session._start_stt_event_task()

    session._schedule_stt_segment_commit(VADStopSpeaking())
    await asyncio.sleep(0.05)

    assert stt.commit_calls == 1
    assert session._turn is not None
    assert session._turn_manager.state == TurnManagerState.USER_PAUSED
    assert session._turn.transcript_text == "hello"


@pytest.mark.asyncio
async def test_handle_end_of_speech_emits_single_turn_level_stt_final_from_segments():
    session = Session(_full_config())
    session._turn = TurnContext("turn-stale", CancelToken())
    session._turn.append_stt_segment("hello")
    session._turn.append_stt_segment("world")

    timeline: list[Event] = []
    session.event_bus.subscribe(STTFinal, lambda e: timeline.append(e))
    session.event_bus.subscribe(AgentFinal, lambda e: timeline.append(e))

    await session._handle_end_of_speech()

    stt_finals = [e for e in timeline if isinstance(e, STTFinal)]
    assert len(stt_finals) == 1
    assert stt_finals[0].text == "hello world"
    agent_finals = [e for e in timeline if isinstance(e, AgentFinal)]
    assert len(agent_finals) == 1
    assert agent_finals[0].text == "HELLO WORLD"


@pytest.mark.asyncio
async def test_run_basic_agent_timeout_clears_turn_id():
    class TimeoutAgent:
        async def run(self, text: str) -> str:
            raise AgentTimeoutError(timeout=0.01)

    session = Session(_full_config(agent=TimeoutAgent()))
    session._turn = TurnContext("turn-stale", CancelToken())

    await session._run_basic_agent("call me at 415-555-2671", token=None)

    assert session._turn is None
    assert session.turn_state == TurnState.IDLE


@pytest.mark.asyncio
async def test_run_basic_agent_tts_error_cleans_turn_state_and_turn_id():
    session = Session(_full_config())
    session._turn = TurnContext("turn-stale", CancelToken())
    session._tts_synth.synthesize = AsyncMock(side_effect=RuntimeError("tts boom"))

    with pytest.raises(RuntimeError, match="tts boom"):
        await session._run_basic_agent("call me at 415-555-2671", token=None)

    # _synthesize_tts's finally block cleans up the turn when playback was started.
    assert session._turn is None


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
    await session._drain_outbound_audio()
    first_turn_marks = list(session._turn.playback_mark_to_bytes.keys())
    assert len(first_turn_marks) == 1

    # ── Second turn (replaces the TurnContext) ──
    session._is_running = True
    with patch.object(session, "_start_stt_event_task"):
        await session._on_turn_started(TurnStarted())
    session._is_running = False

    await session._outbound_queue.put(_make_chunk())
    await session._drain_outbound_audio()
    second_turn_marks = list(session._turn.playback_mark_to_bytes.keys())
    assert len(second_turn_marks) == 1

    # Ack for the second turn's mark works.
    session._on_playback_mark_ack(PlaybackMarkAck(mark_name=second_turn_marks[0]))
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
    await session._drain_outbound_audio()

    canonical_mark = transport.playback_marks[-1]
    session._on_playback_mark_ack(PlaybackMarkAck(mark_name=canonical_mark))

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

    await session._run_basic_agent("call me at 415-555-2671", token=None)

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

    await session._run_basic_agent("call AT&T at 415-555-2671", token=None)

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

    await session._run_basic_agent("Call AT&T at 415-555-2671", token=None)

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

    await session._run_basic_agent("call Siobhan at 415-555-2671", token=None)

    assert tts.payloads
    # provider doesn't support SSML, so we should receive plain text fallback.
    assert tts.payloads[0].format == "plain"
    assert "shi-vawn" in tts.payloads[0].text
    assert "4 1 5" in tts.payloads[0].text
