"""Tests for ``STTCommitter`` extracted from Session in Phase 1."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import TypeVar

import pytest

from easycat._concurrency import RuntimeSupervisor
from easycat._epoch import Epoch, Lease
from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.events import (
    Error,
    ErrorStage,
    EventBus,
    STTEvent,
    STTEventType,
    STTFinal,
    STTPartial,
    VADStartSpeaking,
    VADStopSpeaking,
    WordTimestamp,
)
from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.scope import RuntimeScope
from easycat.session import _stt_committer as stt_committer_module
from easycat.session._journal_sink import SessionJournalSink
from easycat.session._stt_committer import STTCommitter
from easycat.timeouts import STTTimeoutError, TimeoutConfig
from easycat.turn_manager import TurnManager, TurnManagerConfig, TurnManagerState
from tests.session._wiring_helpers import make_wiring

_EventT = TypeVar("_EventT")


class _EmissionLog(list[object]):
    def __init__(self) -> None:
        super().__init__()
        self._changed = asyncio.Event()

    def append(self, event: object) -> None:
        super().append(event)
        self._changed.set()

    async def wait_for(self, event_type: type[_EventT], *, timeout_s: float = 1.0) -> _EventT:
        async def _match() -> _EventT:
            while True:
                for event in self:
                    if isinstance(event, event_type):
                        return event
                self._changed.clear()
                for event in self:
                    if isinstance(event, event_type):
                        return event
                await self._changed.wait()

        return await asyncio.wait_for(_match(), timeout=timeout_s)


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
    capture_identity: Callable[[], Lease[TurnContext | None]] | None = None,
    on_speech_detection_reset=lambda: None,
    stt_track_label=lambda: None,
    turn_config: TurnManagerConfig | None = None,
) -> tuple[STTCommitter, _RecordingSTT, _EmissionLog, TurnContext, TurnManager]:
    stt = stt or _RecordingSTT()
    journal = journal if journal is not None else InMemoryRingBuffer(capacity=64)
    timeout_config = timeout_config or TimeoutConfig()
    bus = EventBus()
    emitted = _EmissionLog()

    async def _emit(event):
        await bus.emit(event)
        emitted.append(event)

    no_turn = TurnContext("no-turn", CancelToken())
    tm = TurnManager(bus, config=turn_config or TurnManagerConfig())
    sink = SessionJournalSink(
        event_bus=bus,
        journal=journal,
        artifact_store=None,
        session_id="sess-1",
        current_turn_id=lambda turn_id=None: turn_id,
    )
    sink.subscribe()

    committer = STTCommitter(
        wiring=make_wiring(
            stt=lambda: stt,
            current_turn=current_turn,
            capture_identity=capture_identity,
            emit=_emit,
            auto_turn_from_stt_final=lambda: auto_turn,
            stt_track_label=stt_track_label,
        ),
        event_bus=bus,
        journal_sink=sink,
        runtime_scope=RuntimeScope.create_root(
            name="stt-committer-test",
            root_id="stt-committer-test",
            supervisor=RuntimeSupervisor(capacity=1),
            survivor_capacity=1,
        ),
        timeout_config=timeout_config,
        segment_silence_ms=segment_silence_ms,
        no_turn=no_turn,
        turn_manager=tm,
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
    with pytest.raises(asyncio.CancelledError):
        await task
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
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_schedule_runs_commit_and_emits_journal_records() -> None:
    journal = InMemoryRingBuffer(capacity=64)
    committer, stt, _emitted, _no_turn, tm = _make_committer(journal=journal, segment_silence_ms=0)
    committer.mark_active()
    tm._state = TurnManagerState.USER_PAUSED
    turn = _new_turn()

    committer.schedule(VADStopSpeaking(), turn=turn)
    pause_task = committer._pause_commit_task
    assert pause_task is not None
    await pause_task
    await committer.await_inflight_commit()
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
async def test_commit_now_rejects_same_state_activity_republication() -> None:
    committer, stt, _emitted, _no_turn, manager = _make_committer()
    committer.mark_active()
    turn = _new_turn()
    manager._state = TurnManagerState.USER_PAUSED
    activity = manager.capture_activity()

    manager._state = TurnManagerState.USER_PAUSED
    await committer.commit_now(turn, activity=activity)

    assert stt.commit_calls == 0
    assert turn.stt_has_uncommitted_audio is True


@pytest.mark.asyncio
async def test_commit_now_uncommitted_reset_when_provider_returns_false() -> None:
    committer, _stt, _emitted, _no_turn, tm = _make_committer(
        stt=_RecordingSTT(commit_result=False)
    )
    committer.mark_active()
    turn = _new_turn()

    await committer.commit_now(turn, pause=tm.capture_pause())

    assert turn.stt_has_uncommitted_audio is True
    assert turn.pending_stt_segment_futures == []  # future was popped
    assert committer._pause_by_future == {}


@pytest.mark.asyncio
async def test_failed_commit_does_not_reopen_audio_after_same_turn_republication() -> None:
    class _BlockingFailedCommitSTT(_RecordingSTT):
        def __init__(self) -> None:
            super().__init__(commit_result=False)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def commit_segment(self) -> bool:
            self.commit_calls += 1
            self.started.set()
            await self.release.wait()
            return False

    turn = _new_turn("same-turn-republication")
    identity: Epoch[TurnContext | None] = Epoch(turn)
    stt = _BlockingFailedCommitSTT()
    committer, _stt, _emitted, _no_turn, _tm = _make_committer(
        stt=stt,
        current_turn=lambda: identity.capture().value,
        capture_identity=identity.capture,
    )
    committer.mark_active()
    commit = asyncio.create_task(committer.commit_now(turn, identity=identity.capture()))
    await stt.started.wait()

    identity.bump(turn)
    stt.release.set()
    await commit

    assert turn.stt_has_uncommitted_audio is False
    assert turn.pending_stt_segment_futures == []


@pytest.mark.asyncio
async def test_failed_commit_does_not_reopen_audio_after_pause_republication() -> None:
    class _BlockingFailedCommitSTT(_RecordingSTT):
        def __init__(self) -> None:
            super().__init__(commit_result=False)
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def commit_segment(self) -> bool:
            self.commit_calls += 1
            self.started.set()
            await self.release.wait()
            return False

    turn = _new_turn("same-pause-republication")
    identity: Epoch[TurnContext | None] = Epoch(turn)
    stt = _BlockingFailedCommitSTT()
    committer, _stt, _emitted, _no_turn, manager = _make_committer(
        stt=stt,
        current_turn=lambda: identity.capture().value,
        capture_identity=identity.capture,
    )
    committer.mark_active()
    manager._state = TurnManagerState.USER_PAUSED
    activity = manager.capture_activity()
    commit = asyncio.create_task(
        committer.commit_now(
            turn,
            identity=identity.capture(),
            activity=activity,
        )
    )
    await stt.started.wait()

    manager._state = TurnManagerState.USER_PAUSED
    stt.release.set()
    await commit

    assert turn.stt_has_uncommitted_audio is False
    assert turn.pending_stt_segment_futures == []


@pytest.mark.asyncio
async def test_stale_event_consumer_rejects_same_turn_republication() -> None:
    turn = _new_turn("same-turn-event-republication")
    identity: Epoch[TurnContext | None] = Epoch(turn)
    stt = _RecordingSTT()
    committer, _stt, emitted, _no_turn, _tm = _make_committer(
        stt=stt,
        current_turn=lambda: identity.capture().value,
        capture_identity=identity.capture,
    )
    lease = identity.capture()
    committer.start_event_loop(turn, identity=lease)
    consumer = committer.stt_task
    assert consumer is not None

    identity.bump(turn)
    await stt._queue.put(STTEvent(type=STTEventType.FINAL, text="stale final"))
    await consumer

    assert turn.stt_segments == []
    assert not any(isinstance(event, STTFinal) for event in emitted)


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
        wiring=make_wiring(stt=lambda: _RecordingSTT(), emit=_emit),
        event_bus=bus,
        journal_sink=sink,
        runtime_scope=RuntimeScope(),
        timeout_config=TimeoutConfig(stt_timeout=0.05),
        segment_silence_ms=0,
        no_turn=no_turn,
        turn_manager=tm,
    )
    turn = _new_turn()
    # Add a pending future that will never resolve.
    turn.pending_stt_segment_futures.append(asyncio.get_running_loop().create_future())

    ok = await committer.await_pending(turn)
    assert ok is False
    stt_errors = [e for e in errors if e.stage == ErrorStage.STT]
    assert stt_errors
    # A provider without version_info() falls back to the generic "stt" label.
    assert stt_errors[0].provider == "stt"


@pytest.mark.asyncio
async def test_await_pending_timeout_does_not_wait_for_resistant_error_subscriber() -> None:
    committer, _stt, _emitted, _no_turn, _tm = _make_committer(
        timeout_config=TimeoutConfig(stt_timeout=0.01),
    )
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()

    async def _resist_cancellation(_event: Error) -> None:
        handler_started.set()
        while not release_handler.is_set():
            try:
                await release_handler.wait()
            except asyncio.CancelledError:
                pass

    committer._event_bus.subscribe(Error, _resist_cancellation)
    turn = _new_turn("resistant-await-pending")
    turn.pending_stt_segment_futures.append(asyncio.get_running_loop().create_future())

    try:
        assert await asyncio.wait_for(committer.await_pending(turn), timeout=0.5) is False
        assert turn.pending_stt_segment_futures == []
        await asyncio.wait_for(handler_started.wait(), timeout=0.5)
        await asyncio.sleep(0.02)
        assert committer._provider_error_supervisor.survivor_count == 1
    finally:
        release_handler.set()
        await committer._provider_error_runtime_scope.drain(suppress_errors=True)


@pytest.mark.asyncio
async def test_await_pending_timeout_error_names_real_provider() -> None:
    class _NamedSTT(_RecordingSTT):
        def version_info(self) -> dict[str, str]:
            return {"provider": "deepgram"}

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
        wiring=make_wiring(stt=lambda: _NamedSTT(), emit=_emit),
        event_bus=bus,
        journal_sink=sink,
        runtime_scope=RuntimeScope(),
        timeout_config=TimeoutConfig(stt_timeout=0.05),
        segment_silence_ms=0,
        no_turn=no_turn,
        turn_manager=tm,
    )
    turn = _new_turn()
    turn.pending_stt_segment_futures.append(asyncio.get_running_loop().create_future())

    ok = await committer.await_pending(turn)
    assert ok is False
    stt_errors = [e for e in errors if e.stage == ErrorStage.STT]
    assert stt_errors
    assert stt_errors[0].provider == "deepgram"
    assert "deepgram" in str(stt_errors[0].exception)


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
async def test_cancel_drains_overlapped_final_close_before_provider_teardown() -> None:
    class _TrackedCloseSTT(_RecordingSTT):
        def __init__(self) -> None:
            super().__init__()
            self.first_started = asyncio.Event()
            self.first_cancelled = asyncio.Event()
            self.active_closes = 0
            self.max_active_closes = 0

        async def end_stream(self) -> None:
            self.end_stream_calls += 1
            self.active_closes += 1
            self.max_active_closes = max(self.max_active_closes, self.active_closes)
            try:
                if self.end_stream_calls == 1:
                    self.first_started.set()
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        self.first_cancelled.set()
                        raise
            finally:
                self.active_closes -= 1

    stt = _TrackedCloseSTT()
    committer, _stt, _emitted, _no_turn, _tm = _make_committer(stt=stt)
    close_task = committer._runtime_scope.create_task(
        committer.FINAL_CLOSE_TASK_NAME,
        stt.end_stream(),
    )
    await asyncio.wait_for(stt.first_started.wait(), timeout=1)

    await committer.cancel(_new_turn())

    assert close_task.cancelled()
    assert stt.first_cancelled.is_set()
    assert stt.end_stream_calls == 2
    assert stt.max_active_closes == 1


@pytest.mark.asyncio
async def test_cancel_logs_when_end_stream_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _RaisingEndStreamSTT(_RecordingSTT):
        async def end_stream(self) -> None:
            self.end_stream_calls += 1
            raise RuntimeError("boom")

    committer, stt, _emitted, _no_turn, _tm = _make_committer(stt=_RaisingEndStreamSTT())
    committer.mark_active()
    turn = _new_turn()

    with caplog.at_level(logging.DEBUG, logger="easycat.session._stt_committer"):
        await committer.cancel(turn)  # must not raise

    assert stt.end_stream_calls == 1
    assert committer.is_active is False
    assert "STT provider lifecycle operation failed" in caplog.text


@pytest.mark.asyncio
async def test_cancel_applies_stt_timeout_and_emits_typed_error() -> None:
    class _HangingEndStreamSTT(_RecordingSTT):
        def __init__(self) -> None:
            super().__init__()
            self.cancelled = asyncio.Event()

        async def end_stream(self) -> None:
            self.end_stream_calls += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    committer, stt, emitted, _no_turn, _tm = _make_committer(
        stt=_HangingEndStreamSTT(),
        timeout_config=TimeoutConfig(stt_timeout=0.02),
    )
    committer.mark_active()

    await asyncio.wait_for(committer.cancel(_new_turn()), timeout=1)

    error = next(event for event in emitted if isinstance(event, Error))
    assert error.stage == ErrorStage.STT
    assert error.provider == "stt"
    assert isinstance(error.exception, STTTimeoutError)
    assert error.exception.timeout == pytest.approx(0.02)
    assert stt.cancelled.is_set()
    assert committer.is_active is False


@pytest.mark.asyncio
async def test_cancel_parks_cancellation_resistant_end_stream_until_it_finishes() -> None:
    class _CancellationResistantEndStreamSTT(_RecordingSTT):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.cancel_requests = 0

        async def end_stream(self) -> None:
            self.end_stream_calls += 1
            self.started.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancel_requests += 1

    stt = _CancellationResistantEndStreamSTT()
    committer, _stt, emitted, _no_turn, _tm = _make_committer(
        stt=stt,
        timeout_config=TimeoutConfig(stt_timeout=0.01),
    )
    committer.mark_active()
    turn = _new_turn()
    cancelling = asyncio.create_task(committer.cancel(turn))

    try:
        await asyncio.wait_for(stt.started.wait(), timeout=1)
        assert await asyncio.wait_for(cancelling, timeout=1) is False

        [owned_end] = committer._runtime_scope.tasks(committer.PROVIDER_END_TASK_NAME)
        assert not owned_end.done()
        assert stt.cancel_requests >= 1
        assert any(
            isinstance(event, Error) and isinstance(event.exception, STTTimeoutError)
            for event in emitted
        )

        stt.release.set()
        await asyncio.wait_for(owned_end, timeout=1)
        await asyncio.sleep(0)

        assert committer._runtime_scope.tasks(committer.PROVIDER_END_TASK_NAME) == ()
        assert await committer.cancel(turn) is True
        assert stt.end_stream_calls == 1
    finally:
        stt.release.set()
        await asyncio.gather(cancelling, return_exceptions=True)
        await committer._runtime_scope.drain(suppress_errors=True)


@pytest.mark.asyncio
async def test_repeated_parked_provider_end_attempts_prune_retired_owner_scopes() -> None:
    class _EventuallySettlingEndStreamSTT(_RecordingSTT):
        async def end_stream(self) -> None:
            self.end_stream_calls += 1
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 0.03
            while loop.time() < deadline:
                try:
                    await asyncio.sleep(deadline - loop.time())
                except asyncio.CancelledError:
                    pass

    stt = _EventuallySettlingEndStreamSTT()
    committer, _stt, _emitted, _no_turn, _tm = _make_committer(
        stt=stt,
        timeout_config=TimeoutConfig(stt_timeout=0.005),
    )
    registry = committer._runtime_scope.survivor_registry
    assert registry is not None

    for index in range(3):
        committer.mark_active()
        assert await committer.cancel(_new_turn(f"turn-{index}")) is False
        [owned_end] = committer._runtime_scope.tasks(committer.PROVIDER_END_TASK_NAME)

        await asyncio.wait_for(owned_end, timeout=1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert committer._runtime_scope.children() == ()
        assert registry.active_count == 0
        assert registry._owner_states == {}

    assert stt.end_stream_calls == 3


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


@pytest.mark.asyncio
async def test_end_stream_times_out_and_resolves_pending_future() -> None:
    class _HangingSTT(_RecordingSTT):
        async def end_stream(self) -> None:
            self.end_stream_calls += 1
            await asyncio.Event().wait()

        def version_info(self) -> dict[str, str]:
            return {"provider": "openai"}

    stt = _HangingSTT()
    committer, _stt, emitted, _no_turn, _tm = _make_committer(
        stt=stt,
        timeout_config=TimeoutConfig(stt_timeout=0.01),
    )
    turn = _new_turn()

    try:
        await committer.end_stream(turn)

        assert stt.end_stream_calls == 1
        assert turn.pending_stt_segment_futures == []
        stt_errors = [e for e in emitted if isinstance(e, Error) and e.stage == ErrorStage.STT]
        assert stt_errors
        assert stt_errors[0].provider == "openai"
    finally:
        await committer._runtime_scope.cancel_and_drain()


@pytest.mark.asyncio
async def test_transferred_provider_close_attempts_are_bounded_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingCloseSTT(_RecordingSTT):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0
            self.fail_close = True

        async def close(self) -> None:
            self.close_calls += 1
            if self.fail_close:
                raise RuntimeError("provider close failed")

    monkeypatch.setattr(stt_committer_module, "_PROVIDER_CLOSE_RETRY_INITIAL_S", 0.0)
    monkeypatch.setattr(stt_committer_module, "_PROVIDER_CLOSE_RETRY_MAX_S", 0.0)
    stt = _FailingCloseSTT()
    committer, _stt, _emitted, _no_turn, _tm = _make_committer(stt=stt)
    committer._provider_close_pending = True

    try:
        await committer._finish_transferred_provider_close()

        assert stt.close_calls == stt_committer_module._PROVIDER_CLOSE_RETRY_ATTEMPTS
        assert committer._provider_close_pending is True
        assert isinstance(committer._provider_close_error, RuntimeError)

        stt.fail_close = False
        assert await committer.retry_transferred_provider_close() is True
        assert committer._provider_close_pending is False
        assert committer._provider_close_error is None
    finally:
        await committer._runtime_scope.cancel_and_drain()


@pytest.mark.asyncio
async def test_end_stream_error_emits_typed_error_and_resolves_pending_future() -> None:
    class _RaisingSTT(_RecordingSTT):
        async def end_stream(self) -> None:
            self.end_stream_calls += 1
            raise RuntimeError("provider close failed")

        def version_info(self) -> dict[str, str]:
            return {"provider": "deepgram"}

    stt = _RaisingSTT()
    committer, _stt, emitted, _no_turn, _tm = _make_committer(stt=stt)
    turn = _new_turn()

    await committer.end_stream(turn)

    assert stt.end_stream_calls == 1
    assert turn.pending_stt_segment_futures == []
    [error] = [event for event in emitted if isinstance(event, Error)]
    assert error.stage == ErrorStage.STT
    assert error.provider == "deepgram"
    assert isinstance(error.exception, RuntimeError)


@pytest.mark.asyncio
async def test_segment_final_journal_records_confidence_and_word_timestamps() -> None:
    """Provider-captured confidence/word timings reach the journal record."""

    class _MetadataSTT(_RecordingSTT):
        async def commit_segment(self) -> bool:
            self.commit_calls += 1
            await self._queue.put(
                STTEvent(
                    type=STTEventType.FINAL,
                    text="hi there",
                    confidence=0.91,
                    word_timestamps=[
                        WordTimestamp(word="hi", start=0.0, end=0.2),
                        WordTimestamp(word="there", start=0.2, end=0.5),
                    ],
                )
            )
            return True

    journal = InMemoryRingBuffer(capacity=64)
    committer, _stt, emitted, _no_turn, _tm = _make_committer(stt=_MetadataSTT(), journal=journal)
    committer.mark_active()
    turn = _new_turn()
    committer.start_event_loop(turn)

    try:
        await committer.commit_now(turn)
        await emitted.wait_for(STTFinal)

        final = next(r for r in journal.read() if r.name == "stt_segment_final")
        assert final.data["confidence"] == 0.91
        assert final.data["word_timestamps"] == [
            {"word": "hi", "start": 0.0, "end": 0.2},
            {"word": "there", "start": 0.2, "end": 0.5},
        ]
    finally:
        await committer.cancel(turn)


@pytest.mark.asyncio
async def test_stt_event_loop_is_runtime_scoped_and_journaled() -> None:
    journal = InMemoryRingBuffer(capacity=64)
    committer, _stt, _emitted, _no_turn, _tm = _make_committer(journal=journal)
    turn = _new_turn("turn-stt-events")

    committer.start_event_loop(turn)
    task = committer.stt_task
    assert task is not None
    assert task in committer._runtime_scope.tasks("stt_event_loop")

    await committer.cancel(turn)

    assert committer.stt_task is None
    assert not committer._runtime_scope.tasks("stt_event_loop")
    records = [
        record for record in journal.read() if record.data.get("task_name") == "stt_event_loop"
    ]
    assert records[0].name == "task_scheduled"
    assert records[0].turn_id == "turn-stt-events"
    assert records[-1].name in {"task_cancelled", "task_completed"}


@pytest.mark.asyncio
async def test_commit_requested_journal_records_pending_commit_bytes() -> None:
    """A PendingCommitReporter STT surfaces its byte count into the journal."""

    class _ReportingSTT(_RecordingSTT):
        def pending_commit_bytes(self) -> int | None:
            return 4800

    journal = InMemoryRingBuffer(capacity=64)
    committer, _stt, _emitted, _no_turn, _tm = _make_committer(
        stt=_ReportingSTT(), journal=journal
    )
    committer.mark_active()
    turn = _new_turn()

    await committer.commit_now(turn)

    requested = next(r for r in journal.read() if r.name == "stt_segment_commit_requested")
    assert requested.data["pending_commit_bytes"] == 4800


@pytest.mark.asyncio
async def test_transport_track_label_stamped_on_unlabeled_final() -> None:
    """An unlabeled provider FINAL gets the transport's inbound track stamped.

    Most STT providers leave ``STTEvent.track`` ``None``; for telephony the
    transport (Twilio) declares an inbound-only capture via ``stt_track_label``
    so the emitted ``STTFinal``/``STTPartial`` carry ``track="inbound"`` and
    downstream telephony classifiers (the voicemail-pickup guard) can trust it.
    """
    stt = _RecordingSTT()
    committer, _stt, emitted, _no_turn, _tm = _make_committer(
        stt=stt, stt_track_label=lambda: "inbound"
    )
    committer.mark_active()
    turn = _new_turn()
    committer.start_event_loop(turn)

    try:
        await stt._queue.put(STTEvent(type=STTEventType.PARTIAL, text="Hel"))
        await stt._queue.put(STTEvent(type=STTEventType.FINAL, text="Hello?"))
        await emitted.wait_for(STTFinal)

        finals = [e for e in emitted if isinstance(e, STTFinal)]
        partials = [e for e in emitted if isinstance(e, STTPartial)]
        assert finals and finals[0].track == "inbound"
        assert partials and partials[0].track == "inbound"
        # The transcript segment recorded on the turn is labelled too.
        assert turn.stt_segments == ["Hello?"]
        assert turn.stt_track == "inbound"
    finally:
        await committer.cancel(turn)


@pytest.mark.asyncio
async def test_non_endpoint_final_does_not_end_native_endpoint_turn() -> None:
    stt = _RecordingSTT()
    committer, _stt, emitted, _no_turn, tm = _make_committer(stt=stt, auto_turn=True)
    committer.mark_active()
    turn = _new_turn()
    ended = asyncio.Event()
    end_calls = 0

    async def record_end_turn() -> None:
        nonlocal end_calls
        end_calls += 1
        ended.set()

    tm.end_turn = record_end_turn  # type: ignore[method-assign]
    committer.start_event_loop(turn)

    try:
        await stt._queue.put(
            STTEvent(type=STTEventType.FINAL, text="reconnect segment", ends_turn=False)
        )
        await emitted.wait_for(STTFinal)
        await asyncio.sleep(0)
        assert end_calls == 0

        await stt._queue.put(STTEvent(type=STTEventType.FINAL, text="native endpoint"))
        await asyncio.wait_for(ended.wait(), timeout=1)
        assert end_calls == 1
    finally:
        await committer.cancel(turn)


@pytest.mark.asyncio
async def test_final_transcript_notifies_turn_manager_endpoint_hint() -> None:
    class _PunctuatedSTT(_RecordingSTT):
        async def commit_segment(self) -> bool:
            self.commit_calls += 1
            await self._queue.put(STTEvent(type=STTEventType.FINAL, text="Complete."))
            return True

    stt = _PunctuatedSTT()
    committer, _stt, emitted, _no_turn, tm = _make_committer(stt=stt)
    committer.mark_active()
    turn = _new_turn()
    committer.start_event_loop(turn)

    try:
        await tm.on_vad_event(VADStartSpeaking())
        await tm.on_vad_event(VADStopSpeaking())
        committer.schedule(VADStopSpeaking(), turn=turn)
        await emitted.wait_for(STTFinal)
        assert tm._punctuated_transcript_event.is_set()
    finally:
        await committer.cancel(turn)
        await tm.shutdown()


@pytest.mark.asyncio
async def test_delayed_final_cannot_shorten_a_later_pause() -> None:
    class _DeferredFinalSTT(_RecordingSTT):
        async def commit_segment(self) -> bool:
            self.commit_calls += 1
            return True

    stt = _DeferredFinalSTT()
    committer, _stt, emitted, _no_turn, tm = _make_committer(
        stt=stt,
        turn_config=TurnManagerConfig(
            end_of_turn_silence_ms=1_000,
            punctuated_end_of_turn_silence_ms=100,
        ),
    )
    committer.mark_active()
    turn = _new_turn()
    committer.start_event_loop(turn)

    try:
        await tm.on_vad_event(VADStartSpeaking())
        await tm.on_vad_event(VADStopSpeaking())
        first_pause = tm.capture_pause()
        committer.schedule(VADStopSpeaking(), turn=turn)
        assert committer._pause_commit_task is not None
        await committer._pause_commit_task
        await committer.await_inflight_commit()

        await tm.on_vad_event(VADStartSpeaking())
        committer.cancel_scheduled(VADStartSpeaking(), turn=turn)
        turn.stt_has_uncommitted_audio = True
        await tm.on_vad_event(VADStopSpeaking())
        second_pause = tm.capture_pause()
        committer.schedule(VADStopSpeaking(), turn=turn)
        assert committer._pause_commit_task is not None
        await committer._pause_commit_task
        await committer.await_inflight_commit()

        assert not first_pause.guard()
        assert second_pause.guard()
        assert len(turn.pending_stt_segment_futures) == 2

        await stt._queue.put(STTEvent(type=STTEventType.FINAL, text="Old segment."))
        await emitted.wait_for(STTFinal)
        assert not tm._punctuated_transcript_event.is_set()

        await stt._queue.put(STTEvent(type=STTEventType.FINAL, text="Current segment."))
        await asyncio.wait_for(tm._punctuated_transcript_event.wait(), timeout=1.0)
        assert committer._pause_by_future == {}
    finally:
        await committer.cancel(turn)
        await tm.shutdown()


@pytest.mark.asyncio
async def test_provider_track_overrides_transport_label() -> None:
    """A provider that stamps its own track is not overwritten by the fallback."""
    stt = _RecordingSTT()
    committer, _stt, emitted, _no_turn, _tm = _make_committer(
        stt=stt, stt_track_label=lambda: "inbound"
    )
    committer.mark_active()
    turn = _new_turn()
    committer.start_event_loop(turn)

    try:
        await stt._queue.put(STTEvent(type=STTEventType.FINAL, text="Hello?", track="caller"))
        await emitted.wait_for(STTFinal)

        finals = [e for e in emitted if isinstance(e, STTFinal)]
        assert finals and finals[0].track == "caller"
    finally:
        await committer.cancel(turn)


@pytest.mark.asyncio
async def test_no_transport_label_leaves_track_none() -> None:
    """With no transport label, unlabeled provider events stay ``track=None``."""
    stt = _RecordingSTT()
    committer, _stt, emitted, _no_turn, _tm = _make_committer(stt=stt)
    committer.mark_active()
    turn = _new_turn()
    committer.start_event_loop(turn)

    try:
        await stt._queue.put(STTEvent(type=STTEventType.FINAL, text="Hello?"))
        await emitted.wait_for(STTFinal)

        finals = [e for e in emitted if isinstance(e, STTFinal)]
        assert finals and finals[0].track is None
    finally:
        await committer.cancel(turn)
