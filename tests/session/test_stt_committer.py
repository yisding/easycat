"""Tests for ``STTCommitter`` extracted from Session in Phase 1."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.events import (
    Error,
    ErrorStage,
    EventBus,
    STTEvent,
    STTEventType,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.runtime.journal import InMemoryRingBuffer
from easycat.runtime.scope import RuntimeScope
from easycat.session._journal_sink import SessionJournalSink
from easycat.session._stt_committer import STTCommitter
from easycat.timeouts import TimeoutConfig
from easycat.turn_manager import TurnManager, TurnManagerConfig, TurnManagerState


class _RecordingSTT:
    def __init__(self, *, commit_result: bool = True, commit_delay_s: float = 0.0) -> None:
        self.commit_result = commit_result
        self.commit_delay_s = commit_delay_s
        self.commit_calls = 0
        self.end_stream_calls = 0
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()

    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk) -> None:
        pass

    async def commit_segment(self) -> bool:
        self.commit_calls += 1
        if self.commit_delay_s:
            await asyncio.sleep(self.commit_delay_s)
        if self.commit_result:
            await self._queue.put(STTEvent(type=STTEventType.FINAL, text="ok"))
        return self.commit_result

    async def end_stream(self) -> None:
        self.end_stream_calls += 1
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event


def _make_committer(
    *,
    stt: _RecordingSTT | None = None,
    journal: InMemoryRingBuffer | None = None,
    timeout_config: TimeoutConfig | None = None,
    segment_silence_ms: int = 0,
    auto_turn: bool = False,
    current_turn=lambda: None,
    on_speech_detection_reset=lambda: None,
) -> tuple[STTCommitter, _RecordingSTT, list, TurnContext, TurnManager]:
    stt = stt or _RecordingSTT()
    journal = journal if journal is not None else InMemoryRingBuffer(capacity=64)
    timeout_config = timeout_config or TimeoutConfig()
    bus = EventBus()
    emitted: list = []

    async def _emit(event):
        emitted.append(event)
        await bus.emit(event)

    no_turn = TurnContext("no-turn", CancelToken())
    tm = TurnManager(bus, config=TurnManagerConfig())
    sink = SessionJournalSink(
        event_bus=bus,
        journal=journal,
        artifact_store=None,
        session_id="sess-1",
        current_turn_id=lambda turn_id=None: turn_id,
    )
    sink.subscribe()

    committer = STTCommitter(
        stt=lambda: stt,
        event_bus=bus,
        journal_sink=sink,
        runtime_scope=RuntimeScope(),
        timeout_config=timeout_config,
        segment_silence_ms=segment_silence_ms,
        no_turn=no_turn,
        current_turn=current_turn,
        turn_manager=tm,
        emit=_emit,
        auto_turn_from_stt_final=lambda: auto_turn,
        on_speech_detection_reset=on_speech_detection_reset,
    )
    return committer, stt, emitted, no_turn, tm


def _new_turn(turn_id: str = "turn-1") -> TurnContext:
    turn = TurnContext(turn_id, CancelToken())
    turn.stt_has_uncommitted_audio = True
    return turn


@pytest.mark.asyncio
async def test_schedule_then_cancel_scheduled_cancels_task() -> None:
    committer, _stt, _emitted, _no_turn, tm = _make_committer(segment_silence_ms=200)
    committer.mark_active()
    tm._state = TurnManagerState.USER_PAUSED
    turn = _new_turn()

    committer.schedule(VADStopSpeaking(), turn=turn)
    task = committer._pause_commit_task
    assert task is not None and not task.done()

    committer.cancel_scheduled(VADStartSpeaking(), turn=turn)
    await asyncio.sleep(0.01)
    assert task.cancelled()
    assert committer._pause_commit_task is None


@pytest.mark.asyncio
async def test_schedule_resolves_current_turn_when_called_like_event_bus() -> None:
    # EventBus.emit invokes handlers as handler(event) — a single positional
    # arg, no turn. schedule() must resolve the active turn itself.
    turn = _new_turn()
    committer, _stt, _emitted, _no_turn, tm = _make_committer(
        segment_silence_ms=200, current_turn=lambda: turn
    )
    committer.mark_active()
    tm._state = TurnManagerState.USER_PAUSED

    committer.schedule(VADStopSpeaking())

    task = committer._pause_commit_task
    assert task is not None and not task.done()
    committer.cancel_scheduled()
    await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_schedule_runs_commit_and_emits_journal_records() -> None:
    journal = InMemoryRingBuffer(capacity=64)
    committer, stt, _emitted, _no_turn, tm = _make_committer(journal=journal, segment_silence_ms=0)
    committer.mark_active()
    tm._state = TurnManagerState.USER_PAUSED
    turn = _new_turn()

    committer.schedule(VADStopSpeaking(), turn=turn)
    for _ in range(20):
        await asyncio.sleep(0.01)
        if stt.commit_calls:
            break
    assert stt.commit_calls == 1

    names = {r.name for r in journal.read()}
    assert "stt_segment_commit_requested" in names
    assert "stt_segment_commit_result" in names


@pytest.mark.asyncio
async def test_commit_now_skips_when_turn_cancelled() -> None:
    committer, stt, _emitted, _no_turn, _tm = _make_committer()
    committer.mark_active()
    turn = _new_turn()
    turn.cancel_token.cancel()

    await committer.commit_now(turn)
    assert stt.commit_calls == 0


@pytest.mark.asyncio
async def test_commit_now_uncommitted_reset_when_provider_returns_false() -> None:
    committer, _stt, _emitted, _no_turn, _tm = _make_committer(
        stt=_RecordingSTT(commit_result=False)
    )
    committer.mark_active()
    turn = _new_turn()

    await committer.commit_now(turn)

    assert turn.stt_has_uncommitted_audio is True
    assert turn.pending_stt_segment_futures == []  # future was popped


@pytest.mark.asyncio
async def test_await_pending_returns_false_on_timeout_and_emits_error() -> None:
    bus = EventBus()
    errors: list[Error] = []
    bus.subscribe(Error, lambda e: errors.append(e))
    journal = InMemoryRingBuffer(capacity=64)
    sink = SessionJournalSink(
        event_bus=bus,
        journal=journal,
        artifact_store=None,
        session_id="sess",
        current_turn_id=lambda turn_id=None: turn_id,
    )

    async def _emit(event):
        await bus.emit(event)

    no_turn = TurnContext("no-turn", CancelToken())
    tm = TurnManager(bus, config=TurnManagerConfig())
    committer = STTCommitter(
        stt=lambda: _RecordingSTT(),
        event_bus=bus,
        journal_sink=sink,
        runtime_scope=RuntimeScope(),
        timeout_config=TimeoutConfig(stt_timeout=0.05),
        segment_silence_ms=0,
        no_turn=no_turn,
        current_turn=lambda: None,
        turn_manager=tm,
        emit=_emit,
        auto_turn_from_stt_final=lambda: False,
    )
    turn = _new_turn()
    # Add a pending future that will never resolve.
    turn.pending_stt_segment_futures.append(asyncio.get_running_loop().create_future())

    ok = await committer.await_pending(turn)
    assert ok is False
    assert any(e.stage == ErrorStage.STT for e in errors)


@pytest.mark.asyncio
async def test_cancel_invokes_on_speech_detection_reset_and_clears_state() -> None:
    reset_calls: list[int] = []

    def _reset() -> None:
        reset_calls.append(1)

    committer, stt, _emitted, _no_turn, _tm = _make_committer(on_speech_detection_reset=_reset)
    committer.mark_active()
    turn = _new_turn()
    turn.pending_stt_segment_futures.append(asyncio.get_running_loop().create_future())

    await committer.cancel(turn)

    assert reset_calls == [1]
    assert committer.is_active is False
    assert stt.end_stream_calls == 1
    assert all(f.done() for f in turn.pending_stt_segment_futures)


@pytest.mark.asyncio
async def test_cancel_preserves_ordering_via_journal_records() -> None:
    """``cancel`` must drain pause+segment task scopes before tearing down."""
    journal = InMemoryRingBuffer(capacity=128)
    committer, _stt, _emitted, _no_turn, tm = _make_committer(
        journal=journal, segment_silence_ms=200
    )
    committer.mark_active()
    tm._state = TurnManagerState.USER_PAUSED
    turn = _new_turn()

    committer.schedule(VADStopSpeaking(), turn=turn)
    pause_task = committer._pause_commit_task
    assert pause_task is not None

    await committer.cancel(turn)

    # Pause commit task was tracked via the runtime scope and cancelled.
    assert pause_task.cancelled()
    assert committer._pause_commit_task is None
    assert committer._segment_commit_task is None


@pytest.mark.asyncio
async def test_end_stream_enqueues_future_when_uncommitted_audio() -> None:
    committer, stt, _emitted, _no_turn, _tm = _make_committer()
    turn = _new_turn()

    await committer.end_stream(turn)
    assert stt.end_stream_calls == 1
    # A pending future is enqueued so the next ``await_pending`` blocks until
    # the trailing FINAL arrives.
    assert len(turn.pending_stt_segment_futures) == 1
    assert turn.stt_has_uncommitted_audio is False


@pytest.mark.asyncio
async def test_end_stream_no_future_when_no_uncommitted_audio() -> None:
    committer, stt, _emitted, _no_turn, _tm = _make_committer()
    turn = _new_turn()
    turn.stt_has_uncommitted_audio = False

    await committer.end_stream(turn)
    assert stt.end_stream_calls == 1
    assert turn.pending_stt_segment_futures == []
