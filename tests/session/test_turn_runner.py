"""Focused unit tests for :class:`TurnRunner`.

These tests exercise the runner via its host Session and verify the
load-bearing per-turn agent loop invariants that survived the Phase 5
decomposition.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import time
import weakref
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from easycat._log_context import CorrelationFilter, bind_turn
from easycat._tts_synthesizer import TTSSynthResult
from easycat._turn_context import TurnContext
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.cancel import CancelToken
from easycat.events import (
    AgentDelta,
    AgentFinal,
    AgentRequestStarted,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    Error,
    ErrorStage,
    Event,
    EventBus,
    STTEvent,
    STTEventType,
    STTFinal,
    ToolCallDelta,
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
from easycat.integrations.agents._agent_runner import AgentRunner, AgentRunnerConfig
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
)
from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.records import JournalRecordKind
from easycat.session._session import Session
from easycat.session._turn_runner import TurnRunner, _StreamingTtsState
from easycat.session._types import SessionConfig
from easycat.session.actions import SessionActions
from easycat.stt.base import STTBase
from easycat.teardown_budgets import (
    SESSION_FORCE_START_LOCK_TIMEOUT_S,
    SESSION_STT_REJECTION_CLEANUP_CANCEL_GRACE_TIMEOUT_S,
    SESSION_STT_REJECTION_CLEANUP_JOIN_TIMEOUT_S,
)
from easycat.timeouts import AgentTimeoutError, STTTimeoutError, TimeoutConfig
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManagerConfig, TurnManagerState, TurnPublication
from tests._bridge_helpers import _TestBridgeBase

_FAST_TURN = TurnManagerConfig(end_of_turn_silence_ms=1)


def test_rejected_stt_cleanup_fits_force_start_lock_budget() -> None:
    assert (
        SESSION_STT_REJECTION_CLEANUP_JOIN_TIMEOUT_S
        + SESSION_STT_REJECTION_CLEANUP_CANCEL_GRACE_TIMEOUT_S
        <= SESSION_FORCE_START_LOCK_TIMEOUT_S
    )


def _preemptive_runner(agent: object, *, timeout: float | None = 30.0) -> AgentRunner:
    return AgentRunner(
        agent,
        AgentRunnerConfig(timeout=timeout, preemptive_generation=True),
    )


# ── Test fakes (mirrors _session_streaming_helpers.py) ────────────


def _chunk(n: int = 320) -> AudioChunk:
    return AudioChunk(data=bytes(n), format=PCM16_MONO_16K)


class FakeTransport:
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

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent.append(chunk)

    async def clear_audio(self) -> None:
        pass


class FakeVAD:
    def __init__(self) -> None:
        self._n = 0

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Event]:
        self._n += 1
        if self._n == 1:
            yield VADStartSpeaking()
        elif self._n == 2:
            yield VADStopSpeaking()

    def configure(self, **kwargs: object) -> None:
        pass


class FakeSTT:
    def __init__(
        self,
        transcript: str = "hello world",
        *,
        fail_on_start: bool = False,
        fail_on_send: bool = False,
    ) -> None:
        self._transcript = transcript
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self._fail_on_start = fail_on_start
        self._fail_on_send = fail_on_send
        self.end_stream_calls = 0
        self.stream_open = False

    async def start_stream(self) -> None:
        if self._fail_on_start:
            raise RuntimeError("STT start_stream failed")
        self.stream_open = True

    async def send_audio(self, chunk: AudioChunk) -> None:
        if self._fail_on_send:
            raise RuntimeError("STT send_audio failed")

    async def end_stream(self) -> None:
        self.end_stream_calls += 1
        self.stream_open = False
        if self._transcript:
            await self._queue.put(STTEvent(type=STTEventType.FINAL, text=self._transcript))
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event


class _CancellationResistantCommitSTT(FakeSTT):
    def __init__(
        self,
        *,
        cleanup_started: asyncio.Event,
        release_cleanup: asyncio.Event,
    ) -> None:
        super().__init__(transcript="")
        self.cleanup_started = cleanup_started
        self.release_cleanup = release_cleanup
        self.start_calls = 0
        self.commit_in_progress = False
        self.concurrent_provider_call = False

    async def start_stream(self) -> None:
        self.concurrent_provider_call |= self.commit_in_progress
        self.start_calls += 1
        if self.stream_open:
            raise RuntimeError("previous STT stream is still open")
        self.stream_open = True

    async def commit_segment(self) -> bool:
        self.commit_in_progress = True
        try:
            while not self.release_cleanup.is_set():
                try:
                    await self.release_cleanup.wait()
                except asyncio.CancelledError:
                    self.cleanup_started.set()
            return False
        finally:
            self.commit_in_progress = False

    async def end_stream(self) -> None:
        self.concurrent_provider_call |= self.commit_in_progress
        await super().end_stream()


class _CancellationResistantLifecycleSTT(STTBase):
    """STTBase double that keeps its lifecycle lock across commit cancellation."""

    def __init__(
        self,
        *,
        cleanup_started: asyncio.Event,
        release_cleanup: asyncio.Event,
    ) -> None:
        super().__init__()
        self.cleanup_started = cleanup_started
        self.release_cleanup = release_cleanup
        self.commit_in_progress = False
        self.close_calls = 0
        self.close_during_commit = False

    async def _on_commit_segment(self) -> bool:
        self.commit_in_progress = True
        try:
            while not self.release_cleanup.is_set():
                try:
                    await self.release_cleanup.wait()
                except asyncio.CancelledError:
                    self.cleanup_started.set()
            return False
        finally:
            self.commit_in_progress = False

    async def close(self) -> None:
        self.close_calls += 1
        self.close_during_commit |= self.commit_in_progress
        await super().close()


class _RetryingTransferredCloseSTT(_CancellationResistantLifecycleSTT):
    def __init__(
        self,
        *,
        cleanup_started: asyncio.Event,
        release_cleanup: asyncio.Event,
        allow_close: asyncio.Event,
    ) -> None:
        super().__init__(
            cleanup_started=cleanup_started,
            release_cleanup=release_cleanup,
        )
        self.allow_close = allow_close
        self.close_failed = asyncio.Event()
        self.close_succeeded = asyncio.Event()

    async def close(self) -> None:
        self.close_calls += 1
        self.close_during_commit |= self.commit_in_progress
        if not self.allow_close.is_set():
            self.close_failed.set()
            raise RuntimeError("transient provider close failure")
        await STTBase.close(self)
        self.close_succeeded.set()


class _GatedTransferredCloseSTT(_CancellationResistantLifecycleSTT):
    def __init__(
        self,
        *,
        cleanup_started: asyncio.Event,
        release_cleanup: asyncio.Event,
    ) -> None:
        super().__init__(
            cleanup_started=cleanup_started,
            release_cleanup=release_cleanup,
        )
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_completed = asyncio.Event()

    async def close(self) -> None:
        self.close_calls += 1
        self.close_during_commit |= self.commit_in_progress
        self.close_started.set()
        await self.release_close.wait()
        await STTBase.close(self)
        self.close_completed.set()


async def _wait_for_stt_timeout(errors: list[Error]) -> Error:
    while True:
        for event in errors:
            if isinstance(event.exception, STTTimeoutError):
                return event
        await asyncio.sleep(0)


def _assert_segment_commit_timeout_handoff(
    session: Session,
    stt: _CancellationResistantCommitSTT,
    scoped_commit: asyncio.Task[None],
    timeout_error: Error,
    resets: list[None],
) -> None:
    assert not scoped_commit.done()
    assert session._runtime_scope.tasks("stt_segment_commit") == (scoped_commit,)
    assert timeout_error.stage is ErrorStage.STT
    assert timeout_error.provider == "stt"
    assert isinstance(timeout_error.exception, STTTimeoutError)
    assert timeout_error.exception.timeout == pytest.approx(0.01)
    assert resets
    assert not stt.concurrent_provider_call
    assert stt.end_stream_calls == 0
    assert stt.start_calls == 1
    assert session._turn is not None
    assert session._turn.id == "old-turn"


def _assert_segment_commit_retry_handoff(
    session: Session,
    stt: _CancellationResistantCommitSTT,
) -> None:
    assert session._runtime_scope.tasks("stt_segment_commit") == ()
    assert not stt.concurrent_provider_call
    assert stt.end_stream_calls == 1
    assert stt.start_calls == 2
    assert session._turn is not None
    assert session._turn.id == "successor-turn-retry"


class FakeTTS:
    def __init__(self) -> None:
        self.synthesized_texts: list[str] = []

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        self.synthesized_texts.append(payload.text)
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


class FakeNoiseReducer:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk


class _SimpleStreamingAgent(_TestBridgeBase):
    """Streams one sentence followed by done."""

    async def run(self, text: str) -> str:
        return "Reply."

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        _ = turn_input, recorder
        yield AgentBridgeEvent(kind="text_delta", text="Reply.")
        yield AgentBridgeEvent(kind="done", text="Reply.")


class _FailingStreamingAgent(_TestBridgeBase):
    async def run(self, text: str) -> str:
        return text

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        _ = turn_input, recorder, cancel_token
        yield AgentBridgeEvent(kind="text_delta", text="")
        raise RuntimeError("agent unavailable")


def _config(**overrides) -> SessionConfig:
    base = {
        "transport": FakeTransport(),
        "vad": FakeVAD(),
        "stt": FakeSTT(transcript="hello"),
        "agent": _SimpleStreamingAgent(),
        "tts": FakeTTS(),
        "noise_reducer": FakeNoiseReducer(),
        "turn_manager_config": _FAST_TURN,
    }
    base.update(overrides)
    return SessionConfig(**base)


def _current_turn_log_context() -> str:
    record = logging.LogRecord(
        name="easycat.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hi",
        args=(),
        exc_info=None,
    )
    CorrelationFilter().filter(record)
    return str(record.turn_id)


# ── Tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_runner_constructed_with_session() -> None:
    """Session installs a TurnRunner instance under ``_turn_runner``."""
    session = Session(_config())
    assert isinstance(session._turn_runner, TurnRunner)
    # The lifecycle command handler is reserved ahead of every public observer.
    handlers = session.event_bus._reserved_handlers.get(TurnStarted, [])
    bound_names = [getattr(h, "__qualname__", "") for h in handlers]
    assert any("TurnRunner.on_turn_started" in name for name in bound_names)
    public_names = [
        getattr(handler, "__qualname__", "")
        for handler in session.event_bus._handlers.get(TurnStarted, [])
    ]
    assert not any("TurnRunner.on_turn_started" in name for name in public_names)
    stt_handlers = session.event_bus._handlers.get(STTFinal, [])
    stt_bound_names = [getattr(h, "__qualname__", "") for h in stt_handlers]
    assert any("TurnRunner.on_stt_final" in name for name in stt_bound_names)


@pytest.mark.asyncio
async def test_on_turn_started_creates_turn_context() -> None:
    """``on_turn_started`` should install a fresh TurnContext on Session."""
    session = Session(_config())
    # Mark the session as 'running' so the gate passes.
    session._is_running = True
    runner = session._turn_runner
    assert session._turn is None

    try:
        await runner.on_turn_started(TurnStarted(turn_id="t-1"))

        assert session._turn is not None
        assert session._turn.id == "t-1"
        assert session._stt_committer.is_active
    finally:
        await session._stt_committer.cancel(session._turn)


@pytest.mark.asyncio
async def test_on_turn_started_does_not_leave_task_bound_to_turn() -> None:
    """Turn startup tags its own logs but does not leak into later task logs."""
    bind_turn(None)
    session = Session(_config())
    session._is_running = True

    try:
        await session._turn_runner.on_turn_started(TurnStarted(turn_id="t-context"))

        assert session._turn is not None
        assert session._turn.id == "t-context"
        assert _current_turn_log_context() == "-"
    finally:
        await session._stt_committer.cancel(session._turn)


@pytest.mark.asyncio
async def test_stale_publication_cannot_replace_successor_after_admission_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session(_config())
    session._is_running = True
    runner = session._turn_runner
    admission_waiting = asyncio.Event()
    release_admission = asyncio.Event()

    async def _wait_during_admission() -> None:
        admission_waiting.set()
        await release_admission.wait()

    monkeypatch.setattr(runner, "cancel_preemptive_generation", _wait_during_admission)
    publication = TurnPublication(
        source="hand_built",
        session_id=session.session_id,
        turn_id="stale-request",
        cancel_token=CancelToken(),
        activity=session._turn_manager.capture_activity(),
    )
    publishing = asyncio.create_task(runner.on_turn_publication(publication))
    await admission_waiting.wait()

    successor = TurnContext("successor", CancelToken())
    runner._turn.set(successor)
    session._turn_manager._state = TurnManagerState.USER_SPEAKING
    release_admission.set()
    await publishing

    assert session._turn is successor
    assert not successor.cancel_token.is_cancelled
    assert not session._stt_committer.is_active


@pytest.mark.asyncio
async def test_successor_turn_closes_stranded_stt_stream_before_starting() -> None:
    """A barge-in cannot start a second stream while the prior close is live."""

    class BlockingLifecycleSTT(STTBase):
        def __init__(self) -> None:
            super().__init__()
            self.start_calls = 0
            self.close_started = asyncio.Event()
            self.release_close = asyncio.Event()

        async def _on_start(self) -> None:
            self.start_calls += 1

        async def _on_end(self) -> None:
            self.close_started.set()
            await self.release_close.wait()

    stt = BlockingLifecycleSTT()
    session = Session(_config(stt=stt))
    session._is_running = True
    runner = session._turn_runner

    try:
        await runner.on_turn_started(TurnStarted(turn_id="old-turn"))
        old_turn = session._turn
        assert old_turn is not None

        # Model the committed-transcript fast path: the old turn has released
        # its active flag, but the provider close task and event consumer still
        # own the prior stream. STTBase correctly refuses a second start until
        # that lifecycle has completed.
        session._stt_committer.mark_inactive()
        close_task = session._runtime_scope.create_task(
            session._stt_committer.FINAL_CLOSE_TASK_NAME,
            session._stt_committer.end_stream(old_turn),
        )
        await asyncio.wait_for(stt.close_started.wait(), timeout=0.25)

        await asyncio.wait_for(
            runner.on_turn_started(TurnStarted(turn_id="successor-turn")),
            timeout=0.25,
        )

        assert close_task.cancelled()
        assert stt.start_calls == 2
        assert session._turn is not None
        assert session._turn.id == "successor-turn"
        assert session._stt_committer.is_active
    finally:
        stt.release_close.set()
        await session._stt_committer.cancel(session._turn)


@pytest.mark.asyncio
async def test_completed_stt_close_does_not_force_successor_teardown() -> None:
    """Drained final-close bookkeeping is not a live successor handoff."""

    class CountingLifecycleSTT(STTBase):
        def __init__(self) -> None:
            super().__init__()
            self.end_calls = 0

        async def _on_end(self) -> None:
            self.end_calls += 1

    stt = CountingLifecycleSTT()
    session = Session(_config(stt=stt))
    session._is_running = True
    runner = session._turn_runner

    try:
        await runner.on_turn_started(TurnStarted(turn_id="old-turn"))
        old_turn = session._turn
        assert old_turn is not None

        session._stt_committer.mark_inactive()
        close_task = session._runtime_scope.create_task(
            session._stt_committer.FINAL_CLOSE_TASK_NAME,
            session._stt_committer.end_stream(old_turn),
        )
        await close_task
        await asyncio.sleep(0)

        assert not session._stt_committer.requires_successor_handoff
        await runner.on_turn_started(TurnStarted(turn_id="successor-turn"))

        assert stt.end_calls == 1
    finally:
        await session._stt_committer.cancel(session._turn)


@pytest.mark.asyncio
async def test_successor_turn_drains_scoped_segment_commit_before_starting() -> None:
    """A detached segment commit still owns its prior STT stream."""

    class StreamOwningSTT(FakeSTT):
        def __init__(self) -> None:
            super().__init__(transcript="")
            self.start_calls = 0

        async def start_stream(self) -> None:
            self.start_calls += 1
            if self.stream_open:
                raise RuntimeError("previous STT stream is still open")
            self.stream_open = True

    stt = StreamOwningSTT()
    session = Session(_config(stt=stt))
    session._is_running = True
    runner = session._turn_runner
    release = asyncio.Event()

    async def _stubborn_segment_commit() -> None:
        await release.wait()

    try:
        await runner.on_turn_started(TurnStarted(turn_id="old-turn"))
        old_turn = session._turn
        assert old_turn is not None

        # Model schedule_turn_ended(): it has cancelled the cached handle,
        # but the runtime still owns an unfinished commit task. Remove the
        # event-consumer signal so this test specifically exercises the
        # scoped-commit handoff guard.
        event_task = session._stt_committer.stt_task
        assert event_task is not None
        event_task.cancel()
        await asyncio.gather(event_task, return_exceptions=True)
        session._stt_committer._stt_task = None
        session._stt_committer.mark_inactive()
        scoped_commit = session._runtime_scope.create_task(
            "stt_segment_commit", _stubborn_segment_commit()
        )
        await asyncio.sleep(0)
        session._stt_committer._segment_commit_task = None

        assert session._stt_committer.requires_successor_handoff
        await runner.on_turn_started(TurnStarted(turn_id="successor-turn"))

        assert scoped_commit.cancelled()
        assert stt.end_stream_calls == 1
        assert stt.start_calls == 2
        assert session._turn is not None
        assert session._turn.id == "successor-turn"
    finally:
        release.set()
        await session._stt_committer.cancel(session._turn)


@pytest.mark.asyncio
async def test_superseded_turn_ended_drains_before_the_successor_runs() -> None:
    """A cancellation-resistant ``on_turn_ended`` must not overlap its successor.

    ``schedule_turn_ended`` cancels the previous end-of-turn task, but it is a
    synchronous handler: it cannot await the unwind, and it then replaced
    ``active_turn_task`` with the new handle. A predecessor that survives the
    cancel therefore kept running while its successor drove another agent turn
    (gh 1024).
    """
    session = Session(_config())
    runner = session._turn_runner
    order: list[str] = []
    release = asyncio.Event()

    async def _stubborn_predecessor() -> None:
        order.append("old:start")
        try:
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Cancellation-resistant: finish the unwind on its own terms.
                await asyncio.shield(release.wait())
        finally:
            order.append("old:done")

    predecessor = session._runtime_scope.create_task("on_turn_ended", _stubborn_predecessor())
    session._tts_scheduler.active_turn_task = predecessor
    await asyncio.sleep(0)
    assert order == ["old:start"]

    runner.schedule_turn_ended(TurnEnded(turn_id="t-successor"))
    successor = session._tts_scheduler.active_turn_task
    assert successor is not None and successor is not predecessor

    await asyncio.sleep(0.05)
    assert predecessor.cancelling() or not predecessor.done()
    # The successor is parked on the drain, so nothing ran past it.
    assert not successor.done()
    assert order == ["old:start"]

    release.set()
    await asyncio.wait_for(successor, timeout=2)

    assert order == ["old:start", "old:done"]
    assert predecessor.done()


@pytest.mark.asyncio
async def test_superseded_turn_ended_drains_the_whole_chain() -> None:
    """A third TurnEnded must still wait on the first, stuck predecessor.

    A drains slowly; B is created and parks on A; a third ``TurnEnded``
    cancels B. B's own wait ends with ``CancelledError`` immediately, which
    says nothing about A — so draining only the handle just replaced would
    let C run while A is still going.
    """
    session = Session(_config())
    runner = session._turn_runner
    order: list[str] = []
    release_a = asyncio.Event()

    async def _stubborn_a() -> None:
        # Resists *every* cancellation until released: the chain cancels A
        # once per superseding TurnEnded.
        order.append("a:start")
        waiter = asyncio.ensure_future(release_a.wait())
        try:
            while not release_a.is_set():
                try:
                    await asyncio.shield(waiter)
                except asyncio.CancelledError:
                    continue
        finally:
            waiter.cancel()
            order.append("a:done")

    task_a = session._runtime_scope.create_task("on_turn_ended", _stubborn_a())
    session._tts_scheduler.active_turn_task = task_a
    await asyncio.sleep(0)
    assert order == ["a:start"]

    # Second TurnEnded: B is created and parks draining A.
    runner.schedule_turn_ended(TurnEnded(turn_id="t-b"))
    task_b = session._tts_scheduler.active_turn_task
    assert task_b is not None and task_b is not task_a
    await asyncio.sleep(0.02)
    assert not task_b.done()

    # Third TurnEnded: B is cancelled, C must still inherit A.
    runner.schedule_turn_ended(TurnEnded(turn_id="t-c"))
    task_c = session._tts_scheduler.active_turn_task
    assert task_c is not None and task_c not in (task_a, task_b)

    try:
        await asyncio.sleep(0.05)
        assert task_b.done()
        # C is parked on A, which is still running.
        assert not task_c.done()
        assert order == ["a:start"]
    finally:
        # Always release, so a regression fails on the assertion rather than
        # hanging the suite on a task that never unwinds.
        release_a.set()

    await asyncio.wait_for(task_c, timeout=2)
    assert order == ["a:start", "a:done"]


@pytest.mark.asyncio
async def test_superseded_turn_ended_drain_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A predecessor that never unwinds must not stall the live turn forever."""
    monkeypatch.setattr("easycat.session._turn_runner._TURN_ENDED_PREDECESSOR_DRAIN_S", 0.02)
    session = Session(_config())
    runner = session._turn_runner
    never_released = asyncio.Event()

    async def _uncancellable_predecessor() -> None:
        # One long-lived inner waiter, cancelled on the way out: a fresh
        # ``shield(sleep(...))`` per iteration would strand orphan tasks that
        # outlive the test and trip the suite's task-leak detector.
        waiter = asyncio.ensure_future(never_released.wait())
        try:
            while not never_released.is_set():
                try:
                    await asyncio.shield(waiter)
                except asyncio.CancelledError:
                    continue
        finally:
            waiter.cancel()

    predecessor = session._runtime_scope.create_task("on_turn_ended", _uncancellable_predecessor())
    session._tts_scheduler.active_turn_task = predecessor
    await asyncio.sleep(0)

    with caplog.at_level(logging.WARNING, logger="easycat.session._turn_runner"):
        runner.schedule_turn_ended(TurnEnded(turn_id="t-successor"))
        successor = session._tts_scheduler.active_turn_task
        assert successor is not None
        await asyncio.wait_for(successor, timeout=2)

    assert "did not unwind" in caplog.text
    never_released.set()
    predecessor.cancel()
    await asyncio.gather(predecessor, return_exceptions=True)


@pytest.mark.asyncio
async def test_successor_turn_bounds_cancellation_resistant_segment_commit() -> None:
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    stt = _CancellationResistantCommitSTT(
        cleanup_started=cleanup_started,
        release_cleanup=release_cleanup,
    )
    session = Session(
        _config(
            stt=stt,
            timeout_config=TimeoutConfig(stt_timeout=0.01),
        )
    )
    session._is_running = True
    runner = session._turn_runner
    resets: list[None] = []
    errors: list[Error] = []
    session._stt_committer._on_speech_detection_reset = lambda: resets.append(None)
    session.event_bus.subscribe(Error, errors.append)

    scoped_commit: asyncio.Task[None] | None = None
    successor: asyncio.Task[None] | None = None
    try:
        await runner.on_turn_started(TurnStarted(turn_id="old-turn"))
        old_turn = session._turn
        assert old_turn is not None
        old_turn.stt_has_uncommitted_audio = True
        await session._stt_committer._start_segment_commit(turn=old_turn)
        scoped_commit = session._stt_committer._segment_commit_task
        assert scoped_commit is not None
        await asyncio.sleep(0)

        event_task = session._stt_committer.stt_task
        assert event_task is not None
        event_task.cancel()
        await asyncio.gather(event_task, return_exceptions=True)
        session._stt_committer._stt_task = None
        session._stt_committer.mark_inactive()
        session._stt_committer._segment_commit_task = None

        successor = asyncio.create_task(
            runner.on_turn_started(TurnStarted(turn_id="successor-turn"))
        )
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)

        timeout_error = await asyncio.wait_for(_wait_for_stt_timeout(errors), timeout=1)
        await asyncio.wait_for(successor, timeout=1)

        assert cleanup_started.is_set()
        _assert_segment_commit_timeout_handoff(
            session,
            stt,
            scoped_commit,
            timeout_error,
            resets,
        )

        release_cleanup.set()
        await asyncio.wait_for(scoped_commit, timeout=1)

        await asyncio.wait_for(
            runner.on_turn_started(TurnStarted(turn_id="successor-turn-retry")),
            timeout=1,
        )

        _assert_segment_commit_retry_handoff(session, stt)
    finally:
        release_cleanup.set()
        if successor is not None:
            await asyncio.gather(successor, return_exceptions=True)
        if scoped_commit is not None:
            await asyncio.gather(scoped_commit, return_exceptions=True)
        await session._stt_committer.cancel(session._turn)


@pytest.mark.asyncio
async def test_rejected_manager_publication_rolls_back_and_retries_after_stt_cleanup() -> None:
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    stt = _CancellationResistantCommitSTT(
        cleanup_started=cleanup_started,
        release_cleanup=release_cleanup,
    )
    session = Session(
        _config(
            stt=stt,
            timeout_config=TimeoutConfig(stt_timeout=0.01),
        )
    )
    session._is_running = True
    observed: list[TurnStarted] = []
    session.event_bus.subscribe(TurnStarted, observed.append)
    scoped_commit: asyncio.Task[None] | None = None

    try:
        await session._turn_runner.on_turn_started(TurnStarted(turn_id="old-turn"))
        old_turn = session._turn
        assert old_turn is not None
        old_turn.stt_has_uncommitted_audio = True
        await session._stt_committer._start_segment_commit(turn=old_turn)
        scoped_commit = session._stt_committer._segment_commit_task
        assert scoped_commit is not None
        await asyncio.sleep(0)

        await asyncio.wait_for(session._turn_manager._begin_turn("test-rejected"), timeout=1)

        assert cleanup_started.is_set()
        assert observed == []
        assert session._turn_manager.state is TurnManagerState.IDLE
        assert session._turn_manager._current_turn_id is None
        assert session._turn is old_turn
        assert stt.start_calls == 1
        assert not stt.concurrent_provider_call

        release_cleanup.set()
        await asyncio.wait_for(scoped_commit, timeout=1)
        await session._turn_manager._begin_turn("test-retry")

        assert session._turn_manager.state is TurnManagerState.USER_SPEAKING
        assert session._turn is not None and session._turn is not old_turn
        assert len(observed) == 1
        assert observed[0].turn_id == session._turn.id
        assert stt.start_calls == 2
        assert not stt.concurrent_provider_call
    finally:
        release_cleanup.set()
        if scoped_commit is not None:
            await asyncio.gather(scoped_commit, return_exceptions=True)
        await session._stt_committer.cancel(session._turn)


@pytest.mark.asyncio
async def test_rejected_start_cleanup_join_is_bounded_when_cleanup_resists_cancellation(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled caller escapes rejected-start cleanup within its join budget.

    Regression test: the post-cancellation join used to loop on
    ``asyncio.shield`` with no deadline, so an STT cleanup that swallowed
    cancellation made the publication caller — and transitively
    ``stop(force=True)`` awaiting the pipeline task — hang forever.
    """
    monkeypatch.setattr("easycat.session._turn_runner._STT_REJECTION_CLEANUP_JOIN_S", 0.05)
    monkeypatch.setattr("easycat.session._turn_runner._STT_REJECTION_CLEANUP_GRACE_S", 0.05)

    session = Session(_config())
    session._is_running = True
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()
    original_cancel = session._stt_committer.cancel

    async def _cancellation_resistant_cancel(turn: TurnContext | None = None) -> bool:
        _ = turn
        cleanup_entered.set()
        while not release_cleanup.is_set():
            try:
                await release_cleanup.wait()
            except asyncio.CancelledError:
                pass
        return True

    def _rejection_cleanup_tasks(turn_id: str) -> list[asyncio.Task[object]]:
        name = f"stt-start-rejection-cleanup-{turn_id}"
        return [task for task in asyncio.all_tasks() if task.get_name() == name]

    driver: asyncio.Task[tuple[bool, asyncio.CancelledError | None]] | None = None
    turn_id = ""
    try:
        await session._turn_runner.on_turn_started(TurnStarted(turn_id="rejected-start"))
        turn = session._turn
        assert turn is not None
        turn_id = turn.id
        monkeypatch.setattr(
            session._stt_committer,
            "cancel",
            _cancellation_resistant_cancel,
        )
        publication = TurnPublication(
            source="voice",
            session_id=session.session_id,
            turn_id=turn.id,
            cancel_token=turn.cancel_token,
            activity=None,
        )
        driver = asyncio.create_task(
            session._turn_runner._cleanup_rejected_stt_start(publication, turn)
        )
        await asyncio.wait_for(cleanup_entered.wait(), timeout=1)

        driver.cancel()
        # The bounded join must let the cancelled caller return promptly even
        # though the cleanup task never settles; pre-fix this awaited forever.
        cleanup_complete, cancellation = await asyncio.wait_for(driver, timeout=1)

        assert cleanup_complete is False
        assert isinstance(cancellation, asyncio.CancelledError)
        assert "exceeded its cancellation join budget" in caplog.text
        orphans = _rejection_cleanup_tasks(turn.id)
        assert len(orphans) == 1
        assert not orphans[0].done()
    finally:
        release_cleanup.set()
        if driver is not None:
            await asyncio.gather(driver, return_exceptions=True)
        for task in _rejection_cleanup_tasks(turn_id):
            await asyncio.gather(task, return_exceptions=True)
        await original_cancel(session._turn)


@pytest.mark.asyncio
async def test_resistant_segment_timeout_notification_allows_rollback_and_force_stop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    error_handler_started = asyncio.Event()
    release_error_handler = asyncio.Event()
    stt = _CancellationResistantCommitSTT(
        cleanup_started=cleanup_started,
        release_cleanup=release_cleanup,
    )
    session = Session(
        _config(
            stt=stt,
            timeout_config=TimeoutConfig(stt_timeout=0.01),
        )
    )
    session._is_running = True

    async def _resist_error_cancellation(_event: Error) -> None:
        error_handler_started.set()
        while not release_error_handler.is_set():
            try:
                await release_error_handler.wait()
            except asyncio.CancelledError:
                pass

    session.event_bus.subscribe(Error, _resist_error_cancellation)
    scoped_commit: asyncio.Task[None] | None = None

    try:
        await session._turn_runner.on_turn_started(TurnStarted(turn_id="old-turn"))
        old_turn = session._turn
        assert old_turn is not None
        old_turn.stt_has_uncommitted_audio = True
        await session._stt_committer._start_segment_commit(turn=old_turn)
        scoped_commit = session._stt_committer._segment_commit_task
        assert scoped_commit is not None
        await asyncio.sleep(0)

        await asyncio.wait_for(session._turn_manager._begin_turn("rejected-turn"), timeout=0.5)

        assert cleanup_started.is_set()
        assert session._turn_manager.state is TurnManagerState.IDLE
        assert session._turn_manager._current_turn_id is None
        assert session._turn is old_turn
        assert not scoped_commit.done()
        await asyncio.wait_for(error_handler_started.wait(), timeout=0.5)
        await asyncio.sleep(0.02)
        assert session._stt_committer._provider_error_supervisor.survivor_count == 1

        # Force stop hits the same segment timeout again. The occupied
        # notification owner rejects that second best-effort Error without
        # delaying survivor parking or provider-close transfer.
        await asyncio.wait_for(session.stop(force=True), timeout=1)

        assert session._closed is True
        assert session._runtime_scope.tasks("stt_segment_commit") == (scoped_commit,)
        assert session._runtime_supervisor.survivor_count == 1
        assert session._stt_committer._provider_error_supervisor.survivor_count == 1
        assert "STT provider Error notification skipped: survivor capacity full" in caplog.text
    finally:
        release_cleanup.set()
        release_error_handler.set()
        if scoped_commit is not None:
            await asyncio.gather(scoped_commit, return_exceptions=True)
        await session._stt_committer._provider_error_runtime_scope.drain(suppress_errors=True)
        if not session._closed:
            await session.stop(force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["cancel_turn", "reset_state"])
async def test_explicit_cancel_preserves_turn_until_stt_handoff_finishes(
    method_name: str,
) -> None:
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    stt = _CancellationResistantCommitSTT(
        cleanup_started=cleanup_started,
        release_cleanup=release_cleanup,
    )
    session = Session(
        _config(
            stt=stt,
            timeout_config=TimeoutConfig(stt_timeout=0.01),
        )
    )
    session._is_running = True
    scoped_commit: asyncio.Task[None] | None = None

    try:
        await session._turn_runner.on_turn_started(TurnStarted(turn_id="old-turn"))
        old_turn = session._turn
        assert old_turn is not None
        old_turn.stt_has_uncommitted_audio = True
        await session._stt_committer._start_segment_commit(turn=old_turn)
        scoped_commit = session._stt_committer._segment_commit_task
        assert scoped_commit is not None
        await asyncio.sleep(0)

        with pytest.raises(RuntimeError, match="lifecycle-owned"):
            await asyncio.wait_for(getattr(session, method_name)(), timeout=1)

        assert cleanup_started.is_set()
        assert session._turn is old_turn
        assert session._stt_committer.requires_successor_handoff

        await session._turn_runner.on_turn_started(TurnStarted(turn_id="rejected-successor"))
        assert session._turn is old_turn
        assert stt.start_calls == 1
        assert not stt.concurrent_provider_call

        release_cleanup.set()
        await asyncio.wait_for(scoped_commit, timeout=1)
        await getattr(session, method_name)()
        assert session._turn is None

        await session._turn_runner.on_turn_started(TurnStarted(turn_id="accepted-successor"))
        assert session._turn is not None
        assert session._turn.id == "accepted-successor"
        assert stt.start_calls == 2
        assert not stt.concurrent_provider_call
    finally:
        release_cleanup.set()
        if scoped_commit is not None:
            await asyncio.gather(scoped_commit, return_exceptions=True)
        await session._stt_committer.cancel(session._turn)


@pytest.mark.asyncio
async def test_force_stop_parks_cancellation_resistant_segment_commit() -> None:
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    stt = _CancellationResistantCommitSTT(
        cleanup_started=cleanup_started,
        release_cleanup=release_cleanup,
    )
    session = Session(
        _config(
            stt=stt,
            timeout_config=TimeoutConfig(stt_timeout=0.01),
        )
    )
    session._is_running = True

    scoped_commit: asyncio.Task[None] | None = None
    try:
        await session._turn_runner.on_turn_started(TurnStarted(turn_id="force-stop-turn"))
        turn = session._turn
        assert turn is not None
        turn.stt_has_uncommitted_audio = True
        await session._stt_committer._start_segment_commit(turn=turn)
        scoped_commit = session._stt_committer._segment_commit_task
        assert scoped_commit is not None
        await asyncio.sleep(0)

        await asyncio.wait_for(session.stop(force=True), timeout=1)

        assert cleanup_started.is_set()
        assert not scoped_commit.done()
        assert session._runtime_scope.tasks("stt_segment_commit") == (scoped_commit,)
        assert session._runtime_supervisor.survivor_count == 1
        assert not stt.concurrent_provider_call
        assert stt.end_stream_calls == 0

        release_cleanup.set()
        await asyncio.wait_for(scoped_commit, timeout=1)
        await asyncio.sleep(0)

        assert session._runtime_scope.tasks("stt_segment_commit") == ()
        assert session._runtime_supervisor.survivor_count == 0
    finally:
        release_cleanup.set()
        if scoped_commit is not None:
            await asyncio.gather(scoped_commit, return_exceptions=True)


@pytest.mark.asyncio
async def test_force_stop_transfers_sttbase_close_behind_parked_commit() -> None:
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    stt = _CancellationResistantLifecycleSTT(
        cleanup_started=cleanup_started,
        release_cleanup=release_cleanup,
    )
    session = Session(
        _config(
            stt=stt,
            timeout_config=TimeoutConfig(stt_timeout=0.01),
        )
    )
    session._is_running = True
    scoped_commit: asyncio.Task[None] | None = None

    try:
        await session._turn_runner.on_turn_started(TurnStarted(turn_id="force-close-turn"))
        turn = session._turn
        assert turn is not None
        turn.stt_has_uncommitted_audio = True
        await session._stt_committer._start_segment_commit(turn=turn)
        scoped_commit = session._stt_committer._segment_commit_task
        assert scoped_commit is not None
        await asyncio.sleep(0)

        await asyncio.wait_for(session.stop(force=True), timeout=1)

        assert cleanup_started.is_set()
        assert not scoped_commit.done()
        assert stt.close_calls == 0
        assert session._runtime_supervisor.survivor_count == 1

        release_cleanup.set()
        await asyncio.wait_for(scoped_commit, timeout=1)
        await asyncio.sleep(0)

        assert stt.close_calls == 1
        assert not stt.close_during_commit
        assert session._runtime_scope.tasks("stt_segment_commit") == ()
        assert session._runtime_supervisor.survivor_count == 0

        await session.stop(force=True)
        assert stt.close_calls == 1
    finally:
        release_cleanup.set()
        if scoped_commit is not None:
            await asyncio.gather(scoped_commit, return_exceptions=True)


@pytest.mark.asyncio
async def test_force_hard_deadline_cannot_cancel_transferred_provider_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    stt = _GatedTransferredCloseSTT(
        cleanup_started=cleanup_started,
        release_cleanup=release_cleanup,
    )
    session = Session(
        _config(
            stt=stt,
            timeout_config=TimeoutConfig(stt_timeout=0.01),
        )
    )
    session._is_running = True
    scoped_commit: asyncio.Task[None] | None = None
    stopping: asyncio.Task[None] | None = None

    try:
        await session._turn_runner.on_turn_started(TurnStarted(turn_id="force-close-window"))
        turn = session._turn
        assert turn is not None
        turn.stt_has_uncommitted_audio = True
        await session._stt_committer._start_segment_commit(turn=turn)
        scoped_commit = session._stt_committer._segment_commit_task
        assert scoped_commit is not None
        await asyncio.sleep(0)

        original_transfer = session._stt_committer.transfer_provider_close_to_owned_work

        def _release_after_transfer() -> bool:
            transferred = original_transfer()
            if transferred:
                release_cleanup.set()
            return transferred

        monkeypatch.setattr(
            session._stt_committer,
            "transfer_provider_close_to_owned_work",
            _release_after_transfer,
        )
        stopping = asyncio.create_task(session.stop(force=True))
        await asyncio.wait_for(stt.close_started.wait(), timeout=1)

        # Keep close suspended beyond the already-started force deadline. A
        # pre-transfer drain must not leave any force cancellation capable of
        # landing inside this provider-owned await.
        await asyncio.sleep(0.03)
        stt.release_close.set()
        await asyncio.wait_for(stopping, timeout=1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert stt.close_completed.is_set()
        assert stt.close_calls == 1
        assert not stt.close_during_commit
        assert session._stt_committer.provider_close_transferred is True
        assert session._stt_committer._provider_close_pending is False
        assert scoped_commit.done() and not scoped_commit.cancelled()
        assert session._runtime_scope.tasks("stt_segment_commit") == ()
        assert session._runtime_supervisor.survivor_count == 0
    finally:
        release_cleanup.set()
        stt.release_close.set()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        if scoped_commit is not None:
            await asyncio.gather(scoped_commit, return_exceptions=True)


@pytest.mark.asyncio
async def test_transferred_provider_close_retry_survives_session_gc(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        "easycat.session._stt_committer._PROVIDER_CLOSE_RETRY_INITIAL_S",
        0.01,
    )
    monkeypatch.setattr(
        "easycat.session._stt_committer._PROVIDER_CLOSE_RETRY_MAX_S",
        0.01,
    )
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    allow_close = asyncio.Event()
    stt = _RetryingTransferredCloseSTT(
        cleanup_started=cleanup_started,
        release_cleanup=release_cleanup,
        allow_close=allow_close,
    )
    session = Session(
        _config(
            stt=stt,
            timeout_config=TimeoutConfig(stt_timeout=0.01),
        )
    )
    session._is_running = True
    scoped_commit: asyncio.Task[None] | None = None

    try:
        await session._turn_runner.on_turn_started(TurnStarted(turn_id="gc-close-turn"))
        turn = session._turn
        assert turn is not None
        turn.stt_has_uncommitted_audio = True
        await session._stt_committer._start_segment_commit(turn=turn)
        scoped_commit = session._stt_committer._segment_commit_task
        assert scoped_commit is not None
        await asyncio.sleep(0)

        await asyncio.wait_for(session.stop(force=True), timeout=1)
        release_cleanup.set()
        await asyncio.wait_for(stt.close_failed.wait(), timeout=1)
        caplog.clear()

        supervisor = session._runtime_supervisor
        session_ref = weakref.ref(session)
        provider_ref = weakref.ref(stt)
        del turn
        del session
        del stt
        gc.collect()

        assert session_ref() is not None
        assert provider_ref() is not None
        assert not scoped_commit.done()
        assert supervisor.survivor_count == 1

        allow_close.set()
        provider = provider_ref()
        assert provider is not None
        await asyncio.wait_for(provider.close_succeeded.wait(), timeout=1)
        del provider
        await asyncio.wait_for(scoped_commit, timeout=1)
        await asyncio.sleep(0)

        for _ in range(3):
            gc.collect()
            await asyncio.sleep(0)

        assert supervisor.survivor_count == 0
    finally:
        release_cleanup.set()
        allow_close.set()
        if scoped_commit is not None:
            await asyncio.gather(scoped_commit, return_exceptions=True)


@pytest.mark.asyncio
async def test_graceful_stop_is_retryable_while_stt_commit_remains_owned() -> None:
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    stt = _CancellationResistantLifecycleSTT(
        cleanup_started=cleanup_started,
        release_cleanup=release_cleanup,
    )
    session = Session(
        _config(
            stt=stt,
            timeout_config=TimeoutConfig(stt_timeout=0.01),
        )
    )
    session._is_running = True
    scoped_commit: asyncio.Task[None] | None = None

    try:
        await session._turn_runner.on_turn_started(TurnStarted(turn_id="graceful-stop-turn"))
        turn = session._turn
        assert turn is not None
        turn.stt_has_uncommitted_audio = True
        await session._stt_committer._start_segment_commit(turn=turn)
        scoped_commit = session._stt_committer._segment_commit_task
        assert scoped_commit is not None
        await asyncio.sleep(0)

        with pytest.raises(RuntimeError, match="lifecycle-owned"):
            await asyncio.wait_for(session.stop(), timeout=1)

        assert cleanup_started.is_set()
        assert session._closed is False
        assert session._stopping is True
        assert session._runtime_scope.tasks("stt_segment_commit") == (scoped_commit,)
        assert stt.close_calls == 0

        release_cleanup.set()
        await asyncio.wait_for(scoped_commit, timeout=1)
        await session.stop()

        assert session._closed is True
        assert session._stopping is False
        assert stt.close_calls == 1
        assert not stt.close_during_commit
        assert session._runtime_scope.tasks("stt_segment_commit") == ()
    finally:
        release_cleanup.set()
        if scoped_commit is not None:
            await asyncio.gather(scoped_commit, return_exceptions=True)


@pytest.mark.asyncio
async def test_manual_turn_before_session_start_is_rejected() -> None:
    session = Session(_config())
    observed: list[TurnStarted] = []
    session.event_bus.subscribe(TurnStarted, observed.append)

    publication = await session._turn_runner.on_turn_publication(
        TurnPublication(
            source="voice",
            session_id=session.session_id,
            turn_id="not-running",
            cancel_token=CancelToken(),
            activity=session._turn_manager.capture_activity(),
        )
    )

    assert publication.admission_rejected is True
    with pytest.raises(RuntimeError, match="not running"):
        await session.start_turn()
    assert observed == []
    assert session._turn_manager.state is TurnManagerState.IDLE
    assert session._turn is None
    assert not session._stt_committer.is_active


@pytest.mark.asyncio
async def test_stale_activity_during_blocked_stt_start_rejects_and_closes_stream() -> None:
    class _BlockingPartialStartSTT(FakeSTT):
        def __init__(self) -> None:
            super().__init__(transcript="")
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def start_stream(self) -> None:
            self.stream_open = True
            self.started.set()
            await self.release.wait()

    stt = _BlockingPartialStartSTT()
    session = Session(_config(stt=stt))
    session._is_running = True
    observed: list[TurnStarted] = []
    session.event_bus.subscribe(TurnStarted, observed.append)
    starting = asyncio.create_task(session._turn_manager._begin_turn("stale-start"))

    await stt.started.wait()
    turn = session._turn
    assert turn is not None
    session._turn_manager.reset()
    stt.release.set()
    await starting

    assert observed == []
    assert session._turn_manager.state is TurnManagerState.IDLE
    assert session._turn is turn
    assert stt.stream_open is False
    assert stt.end_stream_calls == 1
    assert not session._stt_committer.is_active
    session._reset_turn_state()


@pytest.mark.asyncio
async def test_cancellation_during_blocked_stt_start_cleans_up_before_reraising() -> None:
    class _CancellationPartialStartSTT(FakeSTT):
        def __init__(self) -> None:
            super().__init__(transcript="")
            self.started = asyncio.Event()

        async def start_stream(self) -> None:
            self.stream_open = True
            self.started.set()
            await asyncio.Event().wait()

    stt = _CancellationPartialStartSTT()
    session = Session(_config(stt=stt))
    session._is_running = True
    observed: list[TurnStarted] = []
    session.event_bus.subscribe(TurnStarted, observed.append)
    starting = asyncio.create_task(session._turn_manager._begin_turn("cancelled-start"))

    await stt.started.wait()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting

    assert observed == []
    assert session._turn_manager.state is TurnManagerState.IDLE
    assert session._turn is None
    assert stt.stream_open is False
    assert stt.end_stream_calls == 1
    assert not session._stt_committer.is_active


@pytest.mark.asyncio
async def test_cancellation_during_start_error_notification_follows_cleanup() -> None:
    class _PartialStartFailureSTT(FakeSTT):
        async def start_stream(self) -> None:
            self.stream_open = True
            raise RuntimeError("start failed after opening stream")

    stt = _PartialStartFailureSTT(transcript="")
    session = Session(_config(stt=stt))
    session._is_running = True
    error_handler_started = asyncio.Event()

    async def _block_error(_event: Error) -> None:
        error_handler_started.set()
        await asyncio.Event().wait()

    session.event_bus.subscribe(Error, _block_error)
    starting = asyncio.create_task(session._turn_manager._begin_turn("cancel-error-emit"))
    await error_handler_started.wait()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting

    assert session._turn_manager.state is TurnManagerState.IDLE
    assert session._turn_manager._current_turn_id is None
    assert session._turn is None
    assert stt.stream_open is False
    assert stt.end_stream_calls == 1
    assert not session._stt_committer.is_active


@pytest.mark.asyncio
async def test_strict_start_error_subscriber_raises_only_after_cleanup() -> None:
    class _PartialStartFailureSTT(FakeSTT):
        async def start_stream(self) -> None:
            self.stream_open = True
            raise RuntimeError("start failed after opening stream")

    bus = EventBus(handler_error_policy="raise")
    stt = _PartialStartFailureSTT(transcript="")
    session = Session(_config(stt=stt, event_bus=bus))
    session._is_running = True

    def _raise_from_error(_event: Error) -> None:
        raise RuntimeError("strict error subscriber failed")

    bus.subscribe(Error, _raise_from_error)
    with pytest.raises(RuntimeError, match="strict error subscriber failed"):
        await session._turn_manager._begin_turn("strict-error-emit")

    assert session._turn_manager.state is TurnManagerState.IDLE
    assert session._turn_manager._current_turn_id is None
    assert session._turn is None
    assert stt.stream_open is False
    assert stt.end_stream_calls == 1
    assert not session._stt_committer.is_active


@pytest.mark.asyncio
async def test_start_exception_cancelled_during_timed_out_cleanup_rolls_back_manager() -> None:
    class _FailedStartWithStubbornEndSTT(FakeSTT):
        def __init__(self) -> None:
            super().__init__(transcript="")
            self.end_started = asyncio.Event()
            self.release_end = asyncio.Event()

        async def start_stream(self) -> None:
            self.stream_open = True
            raise RuntimeError("start failed after opening stream")

        async def end_stream(self) -> None:
            self.end_stream_calls += 1
            self.end_started.set()
            while not self.release_end.is_set():
                try:
                    await self.release_end.wait()
                except asyncio.CancelledError:
                    pass
            self.stream_open = False

    stt = _FailedStartWithStubbornEndSTT()
    session = Session(
        _config(
            stt=stt,
            timeout_config=TimeoutConfig(stt_timeout=0.01),
        )
    )
    session._is_running = True
    starting = asyncio.create_task(session._turn_manager._begin_turn("error-cancel-timeout"))
    owned_end: asyncio.Task[None] | None = None

    try:
        await stt.end_started.wait()
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(starting, timeout=0.5)

        assert session._turn_manager.state is TurnManagerState.IDLE
        assert session._turn_manager._current_turn_id is None
        assert session._turn is not None
        [owned_end] = session._runtime_scope.tasks(session._stt_committer.PROVIDER_END_TASK_NAME)
        assert not owned_end.done()
        assert session._runtime_supervisor.survivor_count == 1
    finally:
        stt.release_end.set()
        if owned_end is not None:
            await asyncio.gather(owned_end, return_exceptions=True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        session._reset_turn_state()


@pytest.mark.asyncio
async def test_cancelled_start_cleanup_timeout_rolls_back_manager_and_keeps_survivor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _PartialStartWithStubbornEndSTT(FakeSTT):
        def __init__(self) -> None:
            super().__init__(transcript="")
            self.start_started = asyncio.Event()
            self.end_started = asyncio.Event()
            self.release_end = asyncio.Event()

        async def start_stream(self) -> None:
            self.stream_open = True
            self.start_started.set()
            await asyncio.Event().wait()

        async def end_stream(self) -> None:
            self.end_stream_calls += 1
            self.end_started.set()
            while not self.release_end.is_set():
                try:
                    await self.release_end.wait()
                except asyncio.CancelledError:
                    pass
            self.stream_open = False

    stt = _PartialStartWithStubbornEndSTT()
    session = Session(
        _config(
            stt=stt,
            timeout_config=TimeoutConfig(stt_timeout=0.01),
        )
    )
    session._is_running = True
    error_handler_started = asyncio.Event()
    release_error_handler = asyncio.Event()

    async def _resist_error_cancellation(_event: Error) -> None:
        error_handler_started.set()
        while not release_error_handler.is_set():
            try:
                await release_error_handler.wait()
            except asyncio.CancelledError:
                pass

    session.event_bus.subscribe(Error, _resist_error_cancellation)
    starting = asyncio.create_task(session._turn_manager._begin_turn("cancel-timeout"))
    owned_end: asyncio.Task[None] | None = None

    try:
        await stt.start_started.wait()
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting

        assert stt.end_started.is_set()
        assert session._turn_manager.state is TurnManagerState.IDLE
        assert session._turn_manager._current_turn_id is None
        assert session._turn is not None
        assert not session._stt_committer.is_active
        [owned_end] = session._runtime_scope.tasks(session._stt_committer.PROVIDER_END_TASK_NAME)
        assert not owned_end.done()
        assert session._runtime_supervisor.survivor_count == 1

        await asyncio.wait_for(error_handler_started.wait(), timeout=0.5)
        await asyncio.sleep(0.02)
        assert session._stt_committer._provider_error_supervisor.survivor_count == 1

        # The resistant public notification is independently parked and can
        # neither hold manager rollback open nor make force teardown join it.
        await asyncio.wait_for(session.stop(force=True), timeout=1)
        assert session._closed is True
        assert session._stt_committer._provider_error_supervisor.survivor_count == 1
        assert "STT provider Error notification skipped: survivor capacity full" in caplog.text
    finally:
        stt.release_end.set()
        release_error_handler.set()
        if owned_end is not None:
            await asyncio.gather(owned_end, return_exceptions=True)
        await session._stt_committer._provider_error_runtime_scope.drain(suppress_errors=True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        session._reset_turn_state()


@pytest.mark.asyncio
async def test_partial_second_start_failure_closes_new_provider_stream() -> None:
    class _PartialSecondStartFailureSTT(FakeSTT):
        def __init__(self) -> None:
            super().__init__(transcript="")
            self.start_calls = 0

        async def start_stream(self) -> None:
            self.start_calls += 1
            self.stream_open = True
            if self.start_calls == 2:
                raise RuntimeError("second start failed after opening stream")

    stt = _PartialSecondStartFailureSTT()
    session = Session(_config(stt=stt))
    session._is_running = True

    await session._turn_runner.on_turn_started(TurnStarted(turn_id="first"))
    first = session._turn
    assert first is not None
    assert await session._stt_committer.cancel(first) is True
    session._reset_turn_state()
    assert stt.end_stream_calls == 1

    await session._turn_runner.on_turn_started(TurnStarted(turn_id="second"))

    assert stt.start_calls == 2
    assert stt.end_stream_calls == 2
    assert stt.stream_open is False
    assert session._turn is None
    assert not session._stt_committer.is_active


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["start", "preroll"])
async def test_startup_failure_rejects_public_turn_admission(failure_stage: str) -> None:
    stt = FakeSTT(
        transcript="",
        fail_on_start=failure_stage == "start",
        fail_on_send=failure_stage == "preroll",
    )
    session = Session(_config(stt=stt))
    session._is_running = True
    observed: list[TurnStarted] = []
    session.event_bus.subscribe(TurnStarted, observed.append)
    if failure_stage == "preroll":
        session._turn_manager.on_audio_frame(_chunk())

    await session._turn_manager._begin_turn(f"{failure_stage}-failure")

    assert observed == []
    assert session._turn_manager.state is TurnManagerState.IDLE
    assert session._turn_manager._current_turn_id is None
    assert session._turn is None
    assert not session._stt_committer.is_active
    assert stt.end_stream_calls == 1


@pytest.mark.asyncio
async def test_startup_cleanup_timeout_rejects_publication_until_provider_settles() -> None:
    class _StartupFailureWithStubbornEndSTT(FakeSTT):
        def __init__(self) -> None:
            super().__init__(transcript="")
            self.start_calls = 0
            self.cleanup_started = asyncio.Event()
            self.release_cleanup = asyncio.Event()

        async def start_stream(self) -> None:
            self.start_calls += 1
            self.stream_open = True
            if self.start_calls == 1:
                raise RuntimeError("start failed after opening stream")

        async def end_stream(self) -> None:
            self.end_stream_calls += 1
            if self.end_stream_calls == 1:
                self.cleanup_started.set()
                while not self.release_cleanup.is_set():
                    try:
                        await self.release_cleanup.wait()
                    except asyncio.CancelledError:
                        pass
                self.stream_open = False
                return
            await super().end_stream()

    stt = _StartupFailureWithStubbornEndSTT()
    session = Session(
        _config(
            stt=stt,
            timeout_config=TimeoutConfig(stt_timeout=0.01),
        )
    )
    session._is_running = True
    observed: list[TurnStarted] = []
    session.event_bus.subscribe(TurnStarted, observed.append)
    owned_end: asyncio.Task[None] | None = None

    try:
        await session._turn_manager._begin_turn("failed-start")

        assert stt.cleanup_started.is_set()
        assert observed == []
        assert session._turn_manager.state is TurnManagerState.IDLE
        assert session._turn_manager._current_turn_id is None
        assert session._turn is not None
        assert not session._stt_committer.is_active
        [owned_end] = session._runtime_scope.tasks(session._stt_committer.PROVIDER_END_TASK_NAME)
        assert not owned_end.done()

        stt.release_cleanup.set()
        await asyncio.wait_for(owned_end, timeout=1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await session._turn_manager._begin_turn("retry-start")

        assert session._turn_manager.state is TurnManagerState.USER_SPEAKING
        assert session._turn is not None
        assert len(observed) == 1
        assert observed[0].turn_id == session._turn.id
        assert stt.start_calls == 2
    finally:
        stt.release_cleanup.set()
        if owned_end is not None:
            await asyncio.gather(owned_end, return_exceptions=True)
        await session._stt_committer.cancel(session._turn)


@pytest.mark.asyncio
async def test_on_turn_started_start_failure_tears_down_turn() -> None:
    """If ``start_stream`` fails, the turn is fully torn down.

    The turn pointer is reset, STT is marked inactive, no consumer task
    is left pending, and the FSM is returned to IDLE — instead of leaking
    a half-started turn that only the silence timeout could unstick.
    """
    stt = FakeSTT(transcript="hello", fail_on_start=True)
    session = Session(_config(stt=stt))
    session._is_running = True
    errors: list[Error] = []
    session.event_bus.subscribe(Error, lambda e: errors.append(e))

    await session._turn_runner.on_turn_started(TurnStarted(turn_id="t-fail"))

    assert session._turn is None
    assert not session._stt_committer.is_active
    assert session._stt_committer.stt_task is None
    assert session._turn_manager.state == TurnManagerState.IDLE
    assert any(e.stage == ErrorStage.STT for e in errors)


@pytest.mark.asyncio
async def test_stt_start_failure_does_not_reset_republished_activity() -> None:
    class BlockingStartFailureSTT(FakeSTT):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def start_stream(self) -> None:
            self.started.set()
            await self.release.wait()
            raise RuntimeError("late start failure")

    stt = BlockingStartFailureSTT()
    session = Session(_config(stt=stt))
    session._is_running = True
    starting = asyncio.create_task(
        session._turn_runner.on_turn_started(TurnStarted(turn_id="reissued-start"))
    )
    await stt.started.wait()
    turn = session._turn
    assert turn is not None

    session._turn_manager._state = TurnManagerState.IDLE
    stt.release.set()
    await starting

    assert session._turn is turn
    assert session._turn_manager.state is TurnManagerState.IDLE
    session._reset_turn_state()


@pytest.mark.asyncio
async def test_on_turn_started_preroll_failure_tears_down_and_closes_stream() -> None:
    """A failure while priming pre-roll closes the (open) stream and tears down.

    ``start_stream`` succeeds and opens the stream; the failure occurs
    while feeding pre-roll frames. The except path must close the stream
    via ``end_stream`` and leave no orphaned consumer task — the consumer
    loop is now started only *after* priming succeeds.
    """
    stt = FakeSTT(transcript="hello", fail_on_send=True)
    session = Session(_config(stt=stt))

    # Populate and flush the manager's pre-roll directly. Public manual turns
    # now correctly reject admission until Session.start() marks the lifecycle
    # running; this test drives the private publication path below.
    session._turn_manager.on_audio_frame(_chunk())
    session._turn_manager._flush_pre_roll_into_turn_audio()
    assert session._turn_manager.turn_audio  # pre-roll captured

    session._is_running = True
    await session._turn_runner.on_turn_started(TurnStarted(turn_id="t-preroll"))

    assert session._turn is None
    assert not session._stt_committer.is_active
    assert session._stt_committer.stt_task is None
    # The stream that start_stream() opened must be closed on teardown.
    assert stt.end_stream_calls >= 1
    assert stt.stream_open is False
    assert session._turn_manager.state == TurnManagerState.IDLE


@pytest.mark.asyncio
async def test_handle_end_of_speech_empty_transcript_resets() -> None:
    """An empty transcript should skip agent dispatch and reset the turn."""
    session = Session(_config(stt=FakeSTT(transcript="")))
    session._is_running = True
    runner = session._turn_runner
    await runner.on_turn_started(TurnStarted(turn_id="t-empty"))
    turn = session._turn
    assert turn is not None
    # Simulate user finished speaking with no transcript.
    await runner.handle_end_of_speech(turn=turn)
    # Turn pointer is reset.
    assert session._turn is None


@pytest.mark.asyncio
async def test_transcript_finalize_rechecks_identity_after_inflight_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session(_config())
    runner = session._turn_runner
    turn = TurnContext("reissued-during-stt-drain", CancelToken())
    turn.append_stt_segment("ready")
    runner._turn.set(turn)
    session._turn_manager._state = TurnManagerState.PROCESSING
    identity = runner._turn.capture_identity()
    activity = session._turn_manager.capture_activity()
    drain_started = asyncio.Event()
    release_drain = asyncio.Event()

    async def _block_inflight_commit() -> None:
        drain_started.set()
        await release_drain.wait()

    await_pending = AsyncMock(return_value=True)
    monkeypatch.setattr(session._stt_committer, "await_inflight_commit", _block_inflight_commit)
    monkeypatch.setattr(session._stt_committer, "await_pending", await_pending)
    finalizing = asyncio.create_task(
        runner._finalize_turn_transcript(
            turn,
            identity=identity,
            activity=activity,
        )
    )
    await drain_started.wait()

    runner._turn.set(turn)
    release_drain.set()

    assert await finalizing == ""
    assert turn.stt_final_time is None
    await_pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_stt_timeout_closes_stream_before_successor_start() -> None:
    """A timed-out pending final cannot leave the old stream open."""

    class StrictSTT(FakeSTT):
        async def start_stream(self) -> None:
            if self.stream_open:
                raise RuntimeError("second start while prior stream is open")
            await super().start_stream()

    stt = StrictSTT(transcript="")
    session = Session(_config(stt=stt, timeout_config=TimeoutConfig(stt_timeout=0.01)))
    session._is_running = True
    runner = session._turn_runner

    await runner.on_turn_started(TurnStarted(turn_id="first"))
    first = session._turn
    assert first is not None
    assert stt.stream_open
    first.pending_stt_segment_futures.append(asyncio.get_running_loop().create_future())

    await runner.handle_end_of_speech(turn=first)

    assert session._turn is None
    assert not stt.stream_open
    assert stt.end_stream_calls == 1
    assert session._stt_committer.stt_task is None

    await runner.on_turn_started(TurnStarted(turn_id="second"))
    assert session._turn is not None
    await session._stt_committer.cancel(session._turn)


@pytest.mark.asyncio
async def test_vad_stop_commit_waits_for_stop_frame_stt_send() -> None:  # noqa: C901
    """A zero-delay segment commit cannot overlap the stop frame's input write."""

    class ScriptVAD:
        def __init__(self) -> None:
            self.calls = 0

        async def process(self, _chunk: AudioChunk) -> AsyncIterator[Event]:
            self.calls += 1
            if self.calls == 1:
                yield VADStartSpeaking()
            elif self.calls == 2:
                yield VADStopSpeaking()

        def configure(self, **_kwargs: object) -> None:
            pass

    class RaceSTT(FakeSTT):
        def __init__(self) -> None:
            super().__init__(transcript="")
            self.send_count = 0
            self.commit_calls = 0
            self.send_busy = False
            self.send_started = asyncio.Event()
            self.release_send = asyncio.Event()
            self.commit_raced = asyncio.Event()

        async def send_audio(self, chunk: AudioChunk) -> None:
            _ = chunk
            self.send_count += 1
            if self.send_count == 2:
                self.send_busy = True
                self.send_started.set()
                await self.release_send.wait()
                self.send_busy = False

        async def commit_segment(self) -> bool:
            self.commit_calls += 1
            if self.send_busy:
                self.commit_raced.set()
            return False

    stt = RaceSTT()
    session = Session(
        _config(
            stt=stt,
            vad=ScriptVAD(),
            turn_manager_config=TurnManagerConfig(
                end_of_turn_silence_ms=10_000,
                stt_segment_silence_ms=0,
            ),
        )
    )
    session._is_running = True
    chunk = _chunk()
    processing_stop: asyncio.Task[None] | None = None
    try:
        await session._audio_router._process_chunk(chunk)
        processing_stop = asyncio.create_task(session._audio_router._process_chunk(chunk))
        await asyncio.wait_for(stt.send_started.wait(), timeout=1)
        await asyncio.sleep(0)

        assert not stt.commit_raced.is_set()

        stt.release_send.set()
        await processing_stop
        pause_commit = session._stt_committer._pause_commit_task
        assert pause_commit is not None
        await pause_commit
        await session._stt_committer.await_inflight_commit()

        assert stt.commit_calls == 1
        assert not stt.commit_raced.is_set()
    finally:
        stt.release_send.set()
        if processing_stop is not None and not processing_stop.done():
            await processing_stop
        await session._stt_committer.cancel(session._turn)
        await session._turn_manager.shutdown()


@pytest.mark.asyncio
async def test_vad_stop_updates_turn_state_when_stop_frame_stt_send_fails() -> None:
    class FailSecondSendSTT(FakeSTT):
        def __init__(self) -> None:
            super().__init__(transcript="")
            self.send_calls = 0

        async def send_audio(self, chunk: AudioChunk) -> None:
            _ = chunk
            self.send_calls += 1
            if self.send_calls == 2:
                raise RuntimeError("STT send_audio failed")

    stt = FailSecondSendSTT()
    session = Session(
        _config(
            stt=stt,
            turn_manager_config=TurnManagerConfig(
                end_of_turn_silence_ms=10_000,
                stt_segment_silence_ms=10_000,
            ),
        )
    )
    session._is_running = True
    stops: list[VADStopSpeaking] = []
    session.event_bus.subscribe(VADStopSpeaking, stops.append)
    chunk = _chunk()

    try:
        await session._audio_router._process_chunk(chunk)
        with pytest.raises(RuntimeError, match="STT send_audio failed"):
            await session._audio_router._process_chunk(chunk)

        assert len(stops) == 1
        assert session._turn_manager.state is TurnManagerState.USER_PAUSED
    finally:
        await session._stt_committer.cancel(session._turn)
        await session._turn_manager.shutdown()


@pytest.mark.asyncio
async def test_vad_events_after_stop_preserve_order_behind_stt_send() -> None:
    class StopThenStartVAD:
        def __init__(self) -> None:
            self.calls = 0

        async def process(self, _chunk: AudioChunk) -> AsyncIterator[Event]:
            self.calls += 1
            if self.calls == 1:
                yield VADStartSpeaking()
            elif self.calls == 2:
                yield VADStopSpeaking()
                yield VADStartSpeaking()

        def configure(self, **_kwargs: object) -> None:
            pass

    order: list[str] = []

    class OrderingSTT(FakeSTT):
        async def send_audio(self, chunk: AudioChunk) -> None:
            _ = chunk
            order.append("send")

    session = Session(
        _config(
            stt=OrderingSTT(transcript=""),
            vad=StopThenStartVAD(),
            turn_manager_config=TurnManagerConfig(
                end_of_turn_silence_ms=10_000,
                stt_segment_silence_ms=10_000,
            ),
        )
    )
    session._is_running = True
    session.event_bus.subscribe(VADStopSpeaking, lambda _event: order.append("stop"))
    session.event_bus.subscribe(VADStartSpeaking, lambda _event: order.append("start"))
    chunk = _chunk()

    try:
        await session._audio_router._process_chunk(chunk)
        order.clear()

        await session._audio_router._process_chunk(chunk)

        assert order == ["send", "stop", "start"]
        assert session._turn_manager.state is TurnManagerState.USER_SPEAKING
    finally:
        await session._stt_committer.cancel(session._turn)
        await session._turn_manager.shutdown()


@pytest.mark.asyncio
async def test_handle_end_of_speech_dispatches_agent_with_transcript() -> None:
    """A non-empty transcript should produce AgentFinal via the streaming path."""
    session = Session(_config())
    started: list[AgentRequestStarted] = []
    finals: list[AgentFinal] = []
    session.event_bus.subscribe(AgentRequestStarted, lambda e: started.append(e))
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))

    runner = session._turn_runner
    session._turn = TurnContext("turn-x", CancelToken())
    session._turn.append_stt_segment("hello world")
    await runner.handle_end_of_speech(turn=session._turn)

    assert len(started) == 1
    assert started[0].session_id == session.session_id
    assert started[0].turn_id == "turn-x"
    assert len(finals) == 1
    assert finals[0].text == "Reply."


@pytest.mark.asyncio
async def test_committed_transcript_overlaps_stt_close_with_agent() -> None:
    """A provider-confirmed transcript must not wait for stream shutdown."""

    class BlockingCloseSTT(FakeSTT):
        def __init__(self) -> None:
            super().__init__(transcript="")
            self.close_started = asyncio.Event()
            self.release_close = asyncio.Event()

        async def end_stream(self) -> None:
            self.end_stream_calls += 1
            self.close_started.set()
            await self.release_close.wait()
            self.stream_open = False

    class StartedAgent(_SimpleStreamingAgent):
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            _ = turn_input, recorder, cancel_token
            self.started.set()
            yield AgentBridgeEvent(kind="text_delta", text="Reply.")
            yield AgentBridgeEvent(kind="done", text="Reply.")

    stt = BlockingCloseSTT()
    agent = StartedAgent()
    session = Session(_config(stt=stt, agent=agent))
    turn = TurnContext("turn-committed", CancelToken())
    turn.append_stt_segment("hello")
    session._turn = turn
    session._stt_committer.mark_active()

    run_task = asyncio.create_task(session._turn_runner.handle_end_of_speech(turn=turn))
    await asyncio.wait_for(agent.started.wait(), timeout=0.5)

    assert stt.close_started.is_set()
    assert not stt.release_close.is_set()
    assert not run_task.done()

    stt.release_close.set()
    await asyncio.wait_for(run_task, timeout=0.5)
    assert stt.end_stream_calls == 1


@pytest.mark.asyncio
async def test_committed_transcript_fast_path_requires_no_pending_work_or_journal() -> None:
    stt = FakeSTT(transcript="")
    session = Session(_config(stt=stt))
    turn = TurnContext("turn-pending", CancelToken())
    turn.append_stt_segment("hello")
    pending = asyncio.get_running_loop().create_future()
    turn.pending_stt_segment_futures.append(pending)
    session._turn = turn
    session._stt_committer.mark_active()

    transcript, close_task = session._turn_runner._take_committed_transcript(turn)

    assert transcript == ""
    assert close_task is None
    assert session._stt_committer.is_active

    pending.set_result("hello")
    turn.pending_stt_segment_futures.clear()
    journaled = Session(_config(stt=FakeSTT(transcript=""), journal=InMemoryRingBuffer()))
    journaled_turn = TurnContext("turn-journaled", CancelToken())
    journaled_turn.append_stt_segment("hello")
    journaled._turn = journaled_turn
    journaled._stt_committer.mark_active()

    transcript, close_task = journaled._turn_runner._take_committed_transcript(journaled_turn)

    assert transcript == ""
    assert close_task is None
    assert journaled._stt_committer.is_active


@pytest.mark.asyncio
async def test_stt_final_prepares_simple_agent_before_endpoint_confirmation() -> None:
    class BlockingSimpleAgent:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls: list[str] = []

        async def run(self, text: str) -> str:
            self.calls.append(text)
            self.started.set()
            await self.release.wait()
            return f"Reply to {text}."

    agent = BlockingSimpleAgent()
    runner_agent = _preemptive_runner(agent)
    session = Session(_config(agent=runner_agent))
    turn = TurnContext("turn-preemptive", CancelToken())
    session._turn = turn
    turn.append_stt_segment("hello")

    await session._turn_runner.on_stt_final(STTFinal(text="hello", turn_id=turn.id))
    await asyncio.wait_for(agent.started.wait(), timeout=1)
    assert runner_agent.history == []

    agent.release.set()
    task = session._turn_runner._preemptive_task
    assert task is not None
    await asyncio.wait_for(task, timeout=1)
    await session._turn_runner.handle_end_of_speech(turn=turn)

    assert agent.calls == ["hello"]
    assert runner_agent.history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Reply to hello."},
    ]


@pytest.mark.asyncio
async def test_later_stt_final_cancels_and_replaces_preemptive_generation() -> None:
    class RestartableSimpleAgent:
        def __init__(self) -> None:
            self.first_started = asyncio.Event()
            self.first_cancelled = asyncio.Event()
            self.calls: list[str] = []

        async def run(self, text: str) -> str:
            self.calls.append(text)
            if len(self.calls) == 1:
                self.first_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.first_cancelled.set()
                    raise
            return f"Reply to {text}."

    agent = RestartableSimpleAgent()
    runner_agent = _preemptive_runner(agent)
    session = Session(_config(agent=runner_agent))
    turn = TurnContext("turn-restart", CancelToken())
    session._turn = turn
    turn.append_stt_segment("hello")
    await session._turn_runner.on_stt_final(STTFinal(text="hello", turn_id=turn.id))
    await asyncio.wait_for(agent.first_started.wait(), timeout=1)

    turn.append_stt_segment("again")
    await session._turn_runner.on_stt_final(STTFinal(text="again", turn_id=turn.id))
    await asyncio.wait_for(agent.first_cancelled.wait(), timeout=1)
    await session._turn_runner.handle_end_of_speech(turn=turn)

    assert agent.calls == ["hello", "hello again"]
    assert runner_agent.history == [
        {"role": "user", "content": "hello again"},
        {"role": "assistant", "content": "Reply to hello again."},
    ]


@pytest.mark.asyncio
async def test_replaced_turn_does_not_commit_slow_preemptive_response() -> None:
    class SlowSimpleAgent:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls: list[str] = []

        async def run(self, text: str) -> str:
            self.calls.append(text)
            self.started.set()
            await self.release.wait()
            return f"Reply to {text}."

    agent = SlowSimpleAgent()
    runner_agent = _preemptive_runner(agent)
    session = Session(_config(agent=runner_agent))
    old_turn = TurnContext("turn-old", CancelToken())
    session._turn_runner._turn.set(old_turn)
    old_turn.append_stt_segment("abandoned")
    await session._turn_runner.on_stt_final(STTFinal(text="abandoned", turn_id=old_turn.id))
    await asyncio.wait_for(agent.started.wait(), timeout=1)

    old_turn_task = asyncio.create_task(session._turn_runner.handle_end_of_speech(turn=old_turn))
    await asyncio.sleep(0)
    assert session._turn_runner._preemptive_task is None

    old_turn.cancel_token.cancel()
    new_turn = TurnContext("turn-new", CancelToken())
    session._turn_runner._turn.set(new_turn)
    agent.release.set()
    await asyncio.wait_for(old_turn_task, timeout=1)

    assert agent.calls == ["abandoned"]
    assert runner_agent.history == []


@pytest.mark.asyncio
async def test_preemptive_wait_uses_session_timeout_then_runs_confirmed_path() -> None:
    class TimeoutThenRespondAgent:
        def __init__(self) -> None:
            self.first_started = asyncio.Event()
            self.first_cancelled = asyncio.Event()
            self.calls: list[str] = []

        async def run(self, text: str) -> str:
            self.calls.append(text)
            if len(self.calls) == 1:
                self.first_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.first_cancelled.set()
                    raise
            return f"Confirmed reply to {text}."

    agent = TimeoutThenRespondAgent()
    runner_agent = _preemptive_runner(agent, timeout=None)
    session = Session(
        _config(
            agent=runner_agent,
            timeout_config=TimeoutConfig(agent_timeout=0.01),
        )
    )
    errors: list[Error] = []
    session.event_bus.subscribe(Error, errors.append)
    turn = TurnContext("turn-timeout", CancelToken())
    session._turn_runner._turn.set(turn)
    turn.append_stt_segment("hello")
    await session._turn_runner.on_stt_final(STTFinal(text="hello", turn_id=turn.id))
    await asyncio.wait_for(agent.first_started.wait(), timeout=1)

    await asyncio.wait_for(session._turn_runner.handle_end_of_speech(turn=turn), timeout=1)

    assert agent.calls == ["hello", "hello"]
    assert agent.first_cancelled.is_set()
    assert runner_agent.history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Confirmed reply to hello."},
    ]
    assert any(isinstance(event.exception, AgentTimeoutError) for event in errors)


@pytest.mark.asyncio
async def test_preemptive_provider_dispatch_rechecks_identity_when_task_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingAgent:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run(self, text: str) -> str:
            self.calls.append(text)
            return "unused"

    agent = RecordingAgent()
    session = Session(_config(agent=_preemptive_runner(agent)))
    runner = session._turn_runner
    turn = TurnContext("preemptive-task-admission", CancelToken())
    runner._turn.set(turn)
    turn.append_stt_segment("hello")
    scheduled = asyncio.Event()
    release_task = asyncio.Event()
    create_journaled_task = session._runtime_scope.create_journaled_task

    def _delay_task(coro, **kwargs):
        async def _run_after_release():
            scheduled.set()
            await release_task.wait()
            return await coro

        return create_journaled_task(_run_after_release(), **kwargs)

    monkeypatch.setattr(session._runtime_scope, "create_journaled_task", _delay_task)
    await runner.on_stt_final(STTFinal(text="hello", turn_id=turn.id))
    task = runner._preemptive_task
    assert task is not None
    await scheduled.wait()

    runner._turn.set(turn)
    release_task.set()
    await task

    assert agent.calls == []


@pytest.mark.asyncio
async def test_preemptive_wait_propagates_confirmed_turn_cancellation() -> None:
    class BlockingAgent:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def run(self, text: str) -> str:
            _ = text
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    agent = BlockingAgent()
    session = Session(_config(agent=_preemptive_runner(agent, timeout=None)))
    turn = TurnContext("turn-cancel", CancelToken())
    session._turn_runner._turn.set(turn)
    turn.append_stt_segment("cancel now")
    await session._turn_runner.on_stt_final(STTFinal(text="cancel now", turn_id=turn.id))
    await asyncio.wait_for(agent.started.wait(), timeout=1)

    confirmed_task = asyncio.create_task(session._turn_runner.handle_end_of_speech(turn=turn))
    await asyncio.sleep(0)
    confirmed_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await confirmed_task
    assert agent.cancelled.is_set()
    assert session._turn_runner._preemptive_task is None
    assert not session._runtime_scope.tasks(TurnRunner._PREEMPTIVE_TASK_NAME)


@pytest.mark.asyncio
async def test_graceful_stop_cancels_preemptive_generation_before_agent_close() -> None:
    class StopAwareAgent:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def run(self, text: str) -> str:
            _ = text
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    agent = StopAwareAgent()
    session = Session(_config(agent=_preemptive_runner(agent, timeout=None)))
    turn = TurnContext("turn-stop", CancelToken())
    session._turn_runner._turn.set(turn)
    turn.append_stt_segment("stop now")
    await session._turn_runner.on_stt_final(STTFinal(text="stop now", turn_id=turn.id))
    await asyncio.wait_for(agent.started.wait(), timeout=1)

    await asyncio.wait_for(session.stop(), timeout=1)

    assert agent.cancelled.is_set()
    assert session._turn_runner._preemptive_task is None
    assert not session._runtime_scope.tasks(TurnRunner._PREEMPTIVE_TASK_NAME)


@pytest.mark.parametrize("lifecycle_method", ["cancel_turn", "reset_state"])
@pytest.mark.asyncio
async def test_turn_teardown_cancels_preemptive_generation(lifecycle_method: str) -> None:
    class CancellationAwareAgent:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def run(self, text: str) -> str:
            _ = text
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    agent = CancellationAwareAgent()
    session = Session(_config(agent=_preemptive_runner(agent, timeout=None)))
    turn = TurnContext(f"turn-{lifecycle_method}", CancelToken())
    session._turn_runner._turn.set(turn)
    turn.append_stt_segment("cancel now")
    await session._turn_runner.on_stt_final(STTFinal(text="cancel now", turn_id=turn.id))
    await asyncio.wait_for(agent.started.wait(), timeout=1)

    await asyncio.wait_for(getattr(session, lifecycle_method)(), timeout=1)

    assert agent.cancelled.is_set()
    assert session._turn_runner._preemptive_task is None
    assert not session._runtime_scope.tasks(TurnRunner._PREEMPTIVE_TASK_NAME)


class _SlowUnwindAgent:
    """Simple agent whose ``run()`` blocks, then unwinds cancellation slowly.

    On cancellation it sets ``cancel_seen`` and holds the CancelledError
    until ``release`` is set, giving tests a deterministic drain window in
    which the *caller* of a cancel-and-drain helper can itself be cancelled.
    """

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[str] = []

    async def run(self, text: str) -> str:
        self.calls.append(text)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.release.wait()
            raise
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_trailing_stt_final_after_take_does_not_restart_preemptive_generation() -> None:
    """A final flushed during the end-of-speech drain must not spawn a second run().

    Deepgram-style providers can flush two final segments while ``end_stream``
    drains. Once ``handle_end_of_speech`` passes the turn's take point, a
    trailing STTFinal must not start new speculative work that would overlap
    the confirmed ``run()`` for the same turn.
    """

    class BlockingSimpleAgent:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls: list[str] = []

        async def run(self, text: str) -> str:
            self.calls.append(text)
            self.started.set()
            await self.release.wait()
            return f"Reply to {text}."

    agent = BlockingSimpleAgent()
    runner_agent = _preemptive_runner(agent)
    session = Session(_config(agent=runner_agent))
    runner = session._turn_runner
    turn = TurnContext("turn-trailing", CancelToken())
    runner._turn.set(turn)
    turn.append_stt_segment("hello")

    await runner.on_stt_final(STTFinal(text="hello", turn_id=turn.id))
    await asyncio.wait_for(agent.started.wait(), timeout=1)

    eos_task = asyncio.create_task(runner.handle_end_of_speech(turn=turn))
    await asyncio.sleep(0)
    # End-of-speech consumed the speculative task and is awaiting it.
    assert runner._preemptive_task is None

    # Trailing final #2, processed by the still-running STT consumer while
    # the take is in flight, must not start a new speculative attempt.
    await runner.on_stt_final(STTFinal(text="hello", turn_id=turn.id))
    assert runner._preemptive_task is None
    assert agent.calls == ["hello"]

    agent.release.set()
    await asyncio.wait_for(eos_task, timeout=1)

    assert agent.calls == ["hello"]
    assert not session._runtime_scope.tasks(TurnRunner._PREEMPTIVE_TASK_NAME)
    assert runner_agent.history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Reply to hello."},
    ]


@pytest.mark.asyncio
async def test_cancel_preemptive_generation_propagates_host_cancellation() -> None:
    """Cancelling the caller during the drain window must re-raise, not be swallowed."""
    agent = _SlowUnwindAgent()
    session = Session(_config(agent=_preemptive_runner(agent, timeout=None)))
    runner = session._turn_runner
    turn = TurnContext("turn-drain-cancel", CancelToken())
    runner._turn.set(turn)
    turn.append_stt_segment("hello")
    await runner.on_stt_final(STTFinal(text="hello", turn_id=turn.id))
    await asyncio.wait_for(agent.started.wait(), timeout=1)
    task = runner._preemptive_task
    assert task is not None

    host = asyncio.create_task(runner.cancel_preemptive_generation())
    await asyncio.wait_for(agent.cancel_seen.wait(), timeout=1)
    host.cancel()
    agent.release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(host, timeout=1)
    # The drained speculative task itself still ends cancelled and discarded.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert runner._preemptive_task is None
    assert not session._runtime_scope.tasks(TurnRunner._PREEMPTIVE_TASK_NAME)


@pytest.mark.asyncio
async def test_cancel_preemptive_generation_ignores_preexisting_cancellation_count() -> None:
    session = Session(_config())
    runner = session._turn_runner
    started = asyncio.Event()

    async def block() -> None:
        started.set()
        await asyncio.Event().wait()

    owned = session._runtime_scope.create_task(TurnRunner._PREEMPTIVE_TASK_NAME, block())
    runner._preemptive_task = owned
    await started.wait()

    async def cancel_after_caught_cancellation() -> int:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert current.cancelling() == 1

        await runner.cancel_preemptive_generation()
        return current.cancelling()

    caller = asyncio.create_task(cancel_after_caught_cancellation())

    assert await caller == 1
    assert owned.cancelled()
    assert runner._preemptive_task is None
    assert not session._runtime_scope.tasks(TurnRunner._PREEMPTIVE_TASK_NAME)


@pytest.mark.asyncio
async def test_cancel_preemptive_generation_propagates_cancellation_pending_before_entry() -> None:
    session = Session(_config())
    runner = session._turn_runner
    started = asyncio.Event()

    async def block() -> None:
        started.set()
        await asyncio.Event().wait()

    owned = session._runtime_scope.create_task(TurnRunner._PREEMPTIVE_TASK_NAME, block())
    runner._preemptive_task = owned
    await started.wait()

    async def cancel_with_pending_cancellation() -> None:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        await runner.cancel_preemptive_generation()

    caller = asyncio.create_task(cancel_with_pending_cancellation())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert caller.done()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert runner._preemptive_task is owned
    owned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owned


@pytest.mark.asyncio
async def test_empty_transcript_drain_propagates_turn_task_cancellation() -> None:
    """A barge-in's hard cancel of the turn task must survive the empty-transcript drain."""
    agent = _SlowUnwindAgent()
    session = Session(_config(agent=_preemptive_runner(agent, timeout=None)))
    runner = session._turn_runner
    turn = TurnContext("turn-empty-cancel", CancelToken())
    runner._turn.set(turn)
    turn.append_stt_segment("hello")
    await runner.on_stt_final(STTFinal(text="hello", turn_id=turn.id))
    await asyncio.wait_for(agent.started.wait(), timeout=1)
    # The transcript resolves empty at end of speech, so the turn task takes
    # the empty path that only drains the speculative attempt.
    turn.stt_segments.clear()

    eos_task = asyncio.create_task(runner.handle_end_of_speech(turn=turn))
    await asyncio.wait_for(agent.cancel_seen.wait(), timeout=1)
    eos_task.cancel()
    agent.release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(eos_task, timeout=1)
    # The hard-cancelled turn task must not run to completion: the turn
    # pointer is left for the superseding turn to manage, not reset here.
    assert runner._turn.current is turn
    assert runner._preemptive_task is None


@pytest.mark.asyncio
async def test_run_streaming_agent_happy_path_emits_final_and_synthesizes() -> None:
    tts = FakeTTS()
    session = Session(_config(tts=tts))
    finals: list[AgentFinal] = []
    deltas: list[AgentDelta] = []
    session.event_bus.subscribe(AgentFinal, lambda e: finals.append(e))
    session.event_bus.subscribe(AgentDelta, lambda e: deltas.append(e))

    session._turn = TurnContext("turn-happy", CancelToken())
    await session._turn_runner.run_streaming_agent("hello", token=None)

    assert [delta.text for delta in deltas] == ["Reply."]
    assert len(finals) == 1
    assert finals[0].text == "Reply."
    assert tts.synthesized_texts == ["Reply."]


@pytest.mark.asyncio
async def test_done_only_agent_releases_first_tts_payload_gate() -> None:
    class DoneOnlyAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "Done only."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            _ = turn_input, recorder, cancel_token
            yield AgentBridgeEvent(kind="done", text="Done only.")

    tts = FakeTTS()
    session = Session(_config(agent=DoneOnlyAgent(), tts=tts))
    session._turn = TurnContext("turn-done-only", CancelToken())

    await asyncio.wait_for(
        session._turn_runner.run_streaming_agent("hello", token=None), timeout=0.5
    )

    assert tts.synthesized_texts == ["Done only."]


@pytest.mark.asyncio
async def test_run_streaming_agent_ignores_independently_cancelled_tts_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session(_config())
    session._turn = TurnContext("turn-cancelled-tts", CancelToken())

    async def _cancel_tts_consumer(_st: _StreamingTtsState) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(session._turn_runner, "_consume_tts_payloads", _cancel_tts_consumer)

    await session._turn_runner.run_streaming_agent("hello", token=None)


@pytest.mark.asyncio
async def test_stale_tts_settlement_preserves_successor_turn() -> None:
    session = Session(_config())
    runner = session._turn_runner
    old_turn = TurnContext("turn-before-barge-in", CancelToken())
    runner._turn.set(old_turn)
    state = _StreamingTtsState(
        turn=old_turn,
        identity=runner._turn.capture_identity(),
        activity=session._turn_manager.capture_activity(),
        token=old_turn.cancel_token,
        queue=asyncio.Queue(),
    )
    state.synth_started = True
    state.playback_started = False
    state.gated = False
    state.agent_output_settled.set()

    successor = TurnContext("turn-after-barge-in", CancelToken())
    runner._turn.set(successor)
    session._turn_manager._state = TurnManagerState.USER_SPEAKING

    await runner._settle_turn_after_tts(state)

    assert session._turn is successor
    assert session._turn_generation == successor.generation
    assert not successor.cancel_token.is_cancelled
    assert session._turn_manager.state == TurnManagerState.USER_SPEAKING


@pytest.mark.asyncio
async def test_stale_tts_settlement_rejects_same_object_republication() -> None:
    session = Session(_config())
    runner = session._turn_runner
    turn = TurnContext("turn-reissued", CancelToken())
    runner._turn.set(turn)
    state = _StreamingTtsState(
        turn=turn,
        identity=runner._turn.capture_identity(),
        activity=session._turn_manager.capture_activity(),
        token=turn.cancel_token,
        queue=asyncio.Queue(),
    )
    state.synth_started = True
    state.playback_started = False
    state.agent_output_settled.set()

    runner._turn.set(turn)
    session._turn_manager._state = TurnManagerState.USER_SPEAKING

    await runner._settle_turn_after_tts(state)

    assert session._turn is turn
    assert session._turn_manager.state == TurnManagerState.USER_SPEAKING


@pytest.mark.asyncio
async def test_stale_tts_settlement_rejects_same_state_activity_republication() -> None:
    session = Session(_config())
    runner = session._turn_runner
    turn = TurnContext("turn-activity-reissued", CancelToken())
    runner._turn.set(turn)
    session._turn_manager._state = TurnManagerState.BOT_SPEAKING
    state = _StreamingTtsState(
        turn=turn,
        identity=runner._turn.capture_identity(),
        activity=session._turn_manager.capture_activity(),
        token=turn.cancel_token,
        queue=asyncio.Queue(),
    )
    state.synth_started = True
    state.playback_started = True
    state.agent_output_settled.set()

    session._turn_manager._state = TurnManagerState.BOT_SPEAKING
    await runner._settle_turn_after_tts(state)

    assert session._turn is turn
    assert session._turn_manager.state is TurnManagerState.BOT_SPEAKING


@pytest.mark.asyncio
async def test_tts_result_does_not_stamp_republished_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session(_config())
    runner = session._turn_runner
    turn = TurnContext("reissued-during-tts", CancelToken())
    runner._turn.set(turn)
    session._turn_manager._state = TurnManagerState.BOT_SPEAKING
    state = _StreamingTtsState(
        turn=turn,
        identity=runner._turn.capture_identity(),
        activity=session._turn_manager.capture_activity(),
        token=turn.cancel_token,
        queue=asyncio.Queue(),
    )
    state.synth_started = True
    state.queue.put_nowait(TTSInput("Reply."))
    state.queue.put_nowait(None)
    synthesis_started = asyncio.Event()
    release_synthesis = asyncio.Event()

    async def _synthesize(
        _payload: TTSInput,
        _token: CancelToken | None,
        *,
        is_active: object,
    ) -> TTSSynthResult:
        _ = is_active
        synthesis_started.set()
        await release_synthesis.wait()
        return TTSSynthResult(first_audio_time=123.0)

    monkeypatch.setattr(session._tts_scheduler.synthesizer, "synthesize", _synthesize)
    consuming = asyncio.create_task(runner._synthesize_queued_payloads(state))
    await synthesis_started.wait()

    runner._turn.set(turn)
    release_synthesis.set()
    await consuming

    assert turn.first_tts_audio_time is None


@pytest.mark.asyncio
async def test_on_turn_ended_rejects_same_state_activity_republication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session(_config())
    runner = session._turn_runner
    turn = TurnContext("turn-processing-reissued", CancelToken())
    runner._turn.set(turn)
    session._turn_manager._state = TurnManagerState.PROCESSING
    identity = runner._turn.capture_identity()
    activity = session._turn_manager.capture_activity()
    handle_end_of_speech = AsyncMock()
    monkeypatch.setattr(runner, "handle_end_of_speech", handle_end_of_speech)

    session._turn_manager._state = TurnManagerState.PROCESSING
    await runner.on_turn_ended(
        TurnEnded(turn_id=turn.id),
        identity,
        activity,
    )

    handle_end_of_speech.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_streaming_agent_action_drain_triggers_stop() -> None:
    """A drained EndCallAction must call ``session.stop()`` once."""
    actions = SessionActions()
    actions.end_call(reason="done")
    session = Session(_config(session_actions=actions))
    session.stop = AsyncMock()
    session._turn = TurnContext("turn-stop", CancelToken())

    await session._turn_runner.run_streaming_agent("hello", token=None)

    session.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_failure_fallback_keeps_turn_owned_until_tts_settles() -> None:
    tts = FakeTTS()
    session = Session(
        _config(
            agent=_FailingStreamingAgent(),
            tts=tts,
            on_agent_failure="Please try again.",
        )
    )
    turn = TurnContext("turn-fallback", CancelToken())
    session._turn_runner._turn.set(turn)
    session._turn_manager._state = TurnManagerState.PROCESSING
    lifecycle: list[str] = []

    async def _record_started(_event: BotStartedSpeaking) -> None:
        assert session._turn is turn
        assert session._turn_manager.state == TurnManagerState.BOT_SPEAKING
        lifecycle.append("started")

    async def _record_audio(_event: TTSAudio) -> None:
        assert session._turn is turn
        assert session._turn_manager.state == TurnManagerState.BOT_SPEAKING
        lifecycle.append("audio")

    session.event_bus.subscribe(BotStartedSpeaking, _record_started)
    session.event_bus.subscribe(TTSAudio, _record_audio)
    session.event_bus.subscribe(
        BotStoppedSpeaking,
        lambda _event: lifecycle.append("stopped"),
    )

    await session._turn_runner.run_streaming_agent("hello", turn.cancel_token)

    assert tts.synthesized_texts == ["Please try again."]
    assert lifecycle == ["started", "audio", "stopped"]
    assert session._turn is None
    assert session._turn_manager.state == TurnManagerState.IDLE


@pytest.mark.asyncio
async def test_agent_failure_fallback_respects_playback_suppression() -> None:
    tts = FakeTTS()
    session = Session(
        _config(
            agent=_FailingStreamingAgent(),
            tts=tts,
            on_agent_failure="Please try again.",
        )
    )
    turn = TurnContext("turn-suppressed-fallback", CancelToken())
    session._turn_runner._turn.set(turn)
    session._turn_manager._state = TurnManagerState.PROCESSING
    session._tts_scheduler.set_playback_suppressed(True)

    await session._turn_runner.run_streaming_agent("hello", turn.cancel_token)

    assert tts.synthesized_texts == []


class _Gate:
    """Stateful classification gate: buffering until flushed."""

    def __init__(self) -> None:
        self.buffering = True

    def __call__(self) -> bool:
        return self.buffering


@pytest.mark.asyncio
async def test_agent_failure_fallback_respects_classification_gate() -> None:
    gate = _Gate()
    tts = FakeTTS()
    session = Session(
        _config(
            agent=_FailingStreamingAgent(),
            tts=tts,
            audio_gate=gate,
            on_agent_failure="Please try again.",
        )
    )
    turn = TurnContext("turn-gated-fallback", CancelToken())
    session._turn_runner._turn.set(turn)
    session._turn_manager._state = TurnManagerState.PROCESSING
    audio_events: list[TTSAudio] = []
    lifecycle: list[str] = []
    session.event_bus.subscribe(TTSAudio, lambda event: audio_events.append(event))
    session.event_bus.subscribe(
        BotStartedSpeaking,
        lambda _event: lifecycle.append("started"),
    )
    session.event_bus.subscribe(
        BotStoppedSpeaking,
        lambda _event: lifecycle.append("stopped"),
    )

    await session._turn_runner.run_streaming_agent("hello", turn.cancel_token)

    assert tts.synthesized_texts == ["Please try again."]
    assert len(audio_events) == 1
    assert session._outbound_queue.empty()
    assert lifecycle == []
    assert session._turn is turn
    assert session._turn_manager.state == TurnManagerState.IDLE
    assert not turn.cancel_token.is_cancelled


class _GateFlushingTTS(FakeTTS):
    """Flips the gate to flushed right after synthesizing a payload.

    Models the answering-machine/human classifier completing mid-turn:
    ``_is_gated()`` is True when the first TTS payload is snapshotted but
    False by the time the post-loop branch runs.
    """

    def __init__(self, gate: _Gate) -> None:
        super().__init__()
        self._gate = gate

    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        async for event in super().synthesize(payload):
            yield event
        self._gate.buffering = False


@pytest.mark.asyncio
async def test_gated_opener_preserves_turn_when_gate_flushes_mid_synthesis() -> None:
    """Regression: a gated opener whose gate flushes mid-synthesis must
    keep ``session._turn`` alive for gated-replay mark accounting.

    ``_process_tts`` snapshots ``gated`` at first-payload time; re-reading
    ``_is_gated()`` live in the post-loop branch would (after the gate
    flushed) take the ``_reset_turn_state()`` path and null the turn
    pointer the gated replay still needs.
    """
    gate = _Gate()
    tts = _GateFlushingTTS(gate)
    session = Session(_config(tts=tts, audio_gate=gate))
    turn = TurnContext("turn-gated", CancelToken())
    session._turn = turn

    await session._turn_runner.run_streaming_agent("hello", token=None)

    assert tts.synthesized_texts == ["Reply."]  # synthesis ran while gated
    assert gate.buffering is False  # gate flushed mid/post synthesis
    # The turn pointer must survive for gated replay mark accounting.
    assert session._turn is turn


class _CancelMidStreamAgent(_TestBridgeBase):
    """Streams a delta and waits forever — barge-in is observed when cancelled."""

    async def run(self, text: str) -> str:
        return ""

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        _ = turn_input, recorder
        yield AgentBridgeEvent(kind="text_delta", text="Hello. ")
        while True:
            if cancel_token and cancel_token.is_cancelled:
                return
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_barge_in_cancels_streaming_agent() -> None:
    """A cancel-token cancel mid-stream tears down both agent and TTS tasks."""
    tts = FakeTTS()
    session = Session(_config(agent=_CancelMidStreamAgent(), tts=tts))
    token = CancelToken()
    session._turn = TurnContext("turn-barge", token)

    task = asyncio.create_task(session._turn_runner.run_streaming_agent("hello", token=token))
    # Let the agent stream a bit.
    await asyncio.sleep(0.05)
    token.cancel()
    await task
    # No exception, and the agent task is no longer pending.


class _RaisingAgent(_TestBridgeBase):
    async def run(self, text: str) -> str:
        raise RuntimeError("boom")

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        _ = turn_input, recorder, cancel_token
        raise RuntimeError("boom")
        yield  # pragma: no cover


class _FailingAfterDeltaAgent(_TestBridgeBase):
    async def run(self, text: str) -> str:
        return ""

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        _ = turn_input, recorder, cancel_token
        yield AgentBridgeEvent(kind="text_delta", text="Hello.")
        await asyncio.sleep(0)
        raise RuntimeError("agent failed after delta")


class _FailingTTS(FakeTTS):
    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        self.synthesized_texts.append(payload.text)
        raise RuntimeError("tts failed")
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_chunk())  # pragma: no cover


@pytest.mark.asyncio
async def test_agent_failure_emits_error_event() -> None:
    session = Session(_config(agent=_RaisingAgent()))
    errors: list[Error] = []
    session.event_bus.subscribe(Error, lambda e: errors.append(e))

    session._turn = TurnContext("turn-err", CancelToken())
    await session._turn_runner.run_streaming_agent("hello", token=None)

    assert any(e.stage == ErrorStage.AGENT for e in errors)


@pytest.mark.asyncio
async def test_tts_streaming_failure_emits_error_event() -> None:
    session = Session(_config(tts=_FailingTTS()))
    errors: list[Error] = []
    session.event_bus.subscribe(Error, lambda e: errors.append(e))

    session._turn = TurnContext("turn-tts-err", CancelToken())
    await session._turn_runner.run_streaming_agent("hello", token=None)

    assert any(e.stage == ErrorStage.TTS for e in errors)


@pytest.mark.asyncio
async def test_tts_lifecycle_failure_finishes_bot_speaking_turn() -> None:
    session = Session(_config())

    async def _fail_after_bot_start(_event: BotStartedSpeaking) -> None:
        raise RuntimeError("bot start handler failed")

    session.event_bus.subscribe(BotStartedSpeaking, _fail_after_bot_start)
    session._turn = TurnContext("turn-bot-start-err", CancelToken())

    await session._turn_runner.run_streaming_agent("hello", token=None)

    assert session._turn_manager.state == TurnManagerState.IDLE


@pytest.mark.asyncio
async def test_agent_and_tts_streaming_failures_emit_pipeline_exception_group() -> None:
    session = Session(_config(agent=_FailingAfterDeltaAgent(), tts=_FailingTTS()))
    errors: list[Error] = []
    session.event_bus.subscribe(Error, lambda e: errors.append(e))

    session._turn = TurnContext("turn-group-err", CancelToken())
    await session._turn_runner.run_streaming_agent("hello", token=None)

    groups = [
        e.exception
        for e in errors
        if e.stage == ErrorStage.PIPELINE and isinstance(e.exception, ExceptionGroup)
    ]
    assert groups, errors
    group = groups[0]
    assert [type(exc).__name__ for exc in group.exceptions] == ["RuntimeError", "RuntimeError"]
    assert [str(exc) for exc in group.exceptions] == [
        "agent failed after delta",
        "tts failed",
    ]
    assert "stage=agent" in getattr(group.exceptions[0], "__notes__", [])
    assert "stage=tts" in getattr(group.exceptions[1], "__notes__", [])


@pytest.mark.asyncio
async def test_send_text_runs_agent_without_audio() -> None:
    """``send_text`` runs the agent loop without touching audio I/O."""
    session = Session(
        SessionConfig(
            runtime_mode="text_session",
            agent=_SimpleStreamingAgent(),
        )
    )
    response = await session.send_text("hello")
    assert response == "Reply."


@pytest.mark.asyncio
async def test_text_turn_started_observation_never_installs_voice_identity() -> None:
    session = Session(
        SessionConfig(
            runtime_mode="text_session",
            agent=_SimpleStreamingAgent(),
        )
    )
    session._is_running = True
    observed_identity: list[TurnContext | None] = []
    session.event_bus.subscribe(
        TurnStarted, lambda _event: observed_identity.append(session._turn)
    )

    response = await session.send_text("hello")

    assert response == "Reply."
    assert observed_identity == [None]
    assert session.current_turn is None


@pytest.mark.asyncio
async def test_send_text_rechecks_admission_after_waiting_for_prior_work() -> None:
    """A stop that begins mid-admission cannot publish a new text task."""
    session = Session(
        SessionConfig(
            runtime_mode="text_session",
            agent=_SimpleStreamingAgent(),
        )
    )
    entered_cancel = asyncio.Event()
    release_cancel = asyncio.Event()

    async def pause_cancel_application_prompt() -> bool:
        entered_cancel.set()
        await release_cancel.wait()
        return True

    session._turn_runner.cancel_application_prompt = pause_cancel_application_prompt  # type: ignore[method-assign]
    sending = asyncio.create_task(session.send_text("hello"))
    await asyncio.wait_for(entered_cancel.wait(), timeout=0.25)

    session._stopping = True
    release_cancel.set()

    with pytest.raises(RuntimeError, match="Session is stopping"):
        await asyncio.wait_for(sending, timeout=0.25)
    assert session._turn_runner.active_text_turn is None
    assert session._turn_runner.text_turn_cancel_token is None


@pytest.mark.asyncio
async def test_send_text_task_is_runtime_scoped_and_journaled() -> None:
    journal = InMemoryRingBuffer(capacity=64)
    session = Session(
        SessionConfig(
            runtime_mode="text_session",
            agent=_SimpleStreamingAgent(),
            journal=journal,
        )
    )

    response = await session.send_text("hello")

    assert response == "Reply."
    assert session._turn_runner.active_text_turn is None
    assert session._turn_runner.text_turn_cancel_token is None
    assert not session._runtime_scope.tasks(TurnRunner._TEXT_TURN_TASK_NAME)
    records = [
        record
        for record in journal.read()
        if record.data.get("task_name") == TurnRunner._TEXT_TURN_TASK_NAME
    ]
    assert [record.name for record in records] == ["task_scheduled", "task_completed"]
    assert records[0].turn_id is not None
    assert records[0].turn_id == records[1].turn_id


@pytest.mark.asyncio
async def test_send_text_records_total_latency_metric() -> None:
    journal = InMemoryRingBuffer(capacity=64)
    session = Session(
        SessionConfig(
            runtime_mode="text_session",
            agent=_SimpleStreamingAgent(),
            journal=journal,
        )
    )

    assert await session.send_text("hello") == "Reply."

    metric = next(record for record in journal.read() if record.name == "text_turn_latency_ms")
    assert metric.kind == JournalRecordKind.METRIC
    assert metric.data["surface"] == "text_session"
    assert isinstance(metric.data["value"], float)
    # Latency is reported, not gated: no budget tags or alert records.
    assert "latency_budget_exceeded" not in metric.data
    assert not any(record.name == "latency_budget_exceeded" for record in journal.read())


@pytest.mark.asyncio
async def test_send_text_dispatches_tool_events_with_correlation() -> None:
    """The text path translates tool events via the shared dispatch.

    Regression guard for the de-duplication of the bridge-event switch:
    ``_execute_text_turn`` now routes tool_started/tool_delta/tool_result
    through ``emit_tool_event`` (the same translation the voice path uses),
    so the text path must still surface ``ToolCallStarted`` /
    ``ToolCallDelta`` / ``ToolCallResult`` stamped with the text turn's
    session_id and turn_id.
    """

    class _ToolStreamingAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return "Done."

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            _ = turn_input, recorder, cancel_token
            yield AgentBridgeEvent(kind="tool_started", tool_name="lookup", call_id="c1")
            yield AgentBridgeEvent(kind="tool_delta", text="partial", call_id="c1")
            yield AgentBridgeEvent(kind="tool_result", result="42", call_id="c1")
            yield AgentBridgeEvent(kind="text_delta", text="Done.")
            yield AgentBridgeEvent(kind="done", text="Done.")

    session = Session(
        SessionConfig(
            runtime_mode="text_session",
            agent=_ToolStreamingAgent(),
        )
    )
    started: list[ToolCallStarted] = []
    deltas: list[ToolCallDelta] = []
    results: list[ToolCallResult] = []
    session.event_bus.subscribe(ToolCallStarted, lambda e: started.append(e))
    session.event_bus.subscribe(ToolCallDelta, lambda e: deltas.append(e))
    session.event_bus.subscribe(ToolCallResult, lambda e: results.append(e))

    response = await session.send_text("hello")
    assert response == "Done."

    assert len(started) == 1
    assert started[0].tool_name == "lookup"
    assert started[0].call_id == "c1"
    assert len(deltas) == 1
    assert deltas[0].call_id == "c1"
    assert deltas[0].delta == "partial"
    assert len(results) == 1
    assert results[0].call_id == "c1"
    assert results[0].result == "42"

    # Every tool event must carry the text turn's correlation ids — the
    # text path runs outside the TurnManager's active-turn window, so the
    # ids are stamped by the dispatch rather than by Session._with_correlation.
    for event in (started[0], deltas[0], results[0]):
        assert event.session_id == session.session_id
        assert event.turn_id is not None
        assert event.turn_id.startswith("turn-")


@pytest.mark.asyncio
async def test_send_text_clears_turn_log_context_after_turn() -> None:
    """The caller task should not keep the text turn id after send_text returns."""
    bind_turn(None)
    session = Session(
        SessionConfig(
            runtime_mode="text_session",
            agent=_SimpleStreamingAgent(),
        )
    )

    assert await session.send_text("hello") == "Reply."
    assert _current_turn_log_context() == "-"


@pytest.mark.asyncio
async def test_tts_consumer_starts_before_agent_consumer() -> None:
    """The TTS consumer must observe ``BotStartedSpeaking`` before AgentFinal."""
    started: list[str] = []

    session = Session(_config())
    session.event_bus.subscribe(BotStartedSpeaking, lambda _e: started.append("bot"))
    session.event_bus.subscribe(AgentFinal, lambda _e: started.append("final"))

    session._turn = TurnContext("turn-ord", CancelToken())
    await session._turn_runner.run_streaming_agent("hello", token=None)
    # The TTS consumer fires bot_started_speaking on the first non-cancelled
    # payload, which happens before the agent's final event is emitted.
    assert "bot" in started
    assert started.index("bot") < started.index("final")


@pytest.mark.asyncio
async def test_first_tts_provider_overlaps_agent_delta_handler() -> None:
    class StartSignalingTTS(FakeTTS):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
            self.started.set()
            async for event in super().synthesize(payload):
                yield event

    tts = StartSignalingTTS()
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    order: list[str] = []
    session = Session(_config(tts=tts))

    async def _slow_delta_handler(_event: AgentDelta) -> None:
        handler_started.set()
        await release_handler.wait()
        order.append("agent_delta")

    session.event_bus.subscribe(AgentDelta, _slow_delta_handler)
    session.event_bus.subscribe(BotStartedSpeaking, lambda _event: order.append("bot_started"))
    session._turn = TurnContext("turn-overlap-delta", CancelToken())

    run_task = asyncio.create_task(session._turn_runner.run_streaming_agent("hello", token=None))
    try:
        await asyncio.wait_for(handler_started.wait(), timeout=0.5)
        await asyncio.wait_for(tts.started.wait(), timeout=0.5)
        assert order == []
    finally:
        release_handler.set()

    await asyncio.wait_for(run_task, timeout=0.5)
    assert order[:2] == ["agent_delta", "bot_started"]


@pytest.mark.asyncio
async def test_first_tts_lifecycle_rechecks_identity_after_delta_handler() -> None:
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    bot_started: list[BotStartedSpeaking] = []
    session = Session(_config())
    turn = TurnContext("reissued-during-first-tts-lifecycle", CancelToken())
    session._turn_runner._turn.set(turn)

    async def _block_delta(_event: AgentDelta) -> None:
        handler_started.set()
        await release_handler.wait()

    session.event_bus.subscribe(AgentDelta, _block_delta)
    session.event_bus.subscribe(BotStartedSpeaking, bot_started.append)
    running = asyncio.create_task(session._turn_runner.run_streaming_agent("hello", token=None))
    await handler_started.wait()

    session._turn_runner._turn.set(turn)
    release_handler.set()
    await running

    assert bot_started == []
    assert session._turn_manager.state is TurnManagerState.IDLE


@pytest.mark.asyncio
async def test_voice_stream_suppresses_output_yielded_after_identity_republication() -> None:
    class DelayedDeltaAgent(_TestBridgeBase):
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self, text: str) -> str:
            return text

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            _ = turn_input, recorder, cancel_token
            self.started.set()
            await self.release.wait()
            yield AgentBridgeEvent(kind="text_delta", text="stale")
            yield AgentBridgeEvent(kind="done", text="stale")

    agent = DelayedDeltaAgent()
    tts = FakeTTS()
    session = Session(_config(agent=agent, tts=tts))
    turn = TurnContext("reissued-before-agent-delta", CancelToken())
    session._turn_runner._turn.set(turn)
    output: list[Event] = []
    session.event_bus.subscribe(AgentDelta, output.append)
    session.event_bus.subscribe(AgentFinal, output.append)
    running = asyncio.create_task(session._turn_runner.run_streaming_agent("hello", token=None))
    await agent.started.wait()

    session._turn_runner._turn.set(turn)
    agent.release.set()

    assert await running == ""
    assert output == []
    assert tts.synthesized_texts == []


@pytest.mark.asyncio
async def test_first_tts_provider_starts_before_terminal_agent_event() -> None:
    class StartSignalingTTS(FakeTTS):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
            self.started.set()
            async for event in super().synthesize(payload):
                yield event

    tts = StartSignalingTTS()
    provider_started_before_done = False

    class OrderingAgent(_SimpleStreamingAgent):
        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            nonlocal provider_started_before_done
            _ = turn_input, recorder, cancel_token
            yield AgentBridgeEvent(kind="text_delta", text="Reply.")
            provider_started_before_done = tts.started.is_set()
            yield AgentBridgeEvent(kind="done", text="Reply.")

    session = Session(_config(tts=tts, agent=OrderingAgent()))
    session._turn = TurnContext("turn-prioritize-tts", CancelToken())

    await session._turn_runner.run_streaming_agent("hello", token=None)

    assert provider_started_before_done is True


@pytest.mark.asyncio
async def test_agent_delta_handler_failure_rejects_speculative_tts() -> None:
    class StartSignalingTTS(FakeTTS):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
            self.started.set()
            async for event in super().synthesize(payload):
                yield event

    tts = StartSignalingTTS()
    event_bus = EventBus(handler_error_policy="raise")
    bot_started: list[BotStartedSpeaking] = []
    audio: list[TTSAudio] = []
    errors: list[Error] = []
    session = Session(_config(tts=tts, event_bus=event_bus))

    async def _fail_delta_handler(_event: AgentDelta) -> None:
        raise RuntimeError("delta handler failed")

    event_bus.subscribe(AgentDelta, _fail_delta_handler)
    event_bus.subscribe(BotStartedSpeaking, bot_started.append)
    event_bus.subscribe(TTSAudio, audio.append)
    event_bus.subscribe(Error, errors.append)
    session._turn = TurnContext("turn-reject-delta", CancelToken())

    await asyncio.wait_for(
        session._turn_runner.run_streaming_agent("hello", token=None), timeout=0.5
    )

    assert tts.started.is_set()
    assert bot_started == []
    assert audio == []
    assert any(event.stage == ErrorStage.AGENT for event in errors)


@pytest.mark.asyncio
async def test_first_tts_lifecycle_wait_is_outside_agent_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat.session._turn_runner as turn_runner_module

    lifecycle_started = asyncio.Event()
    release_lifecycle = asyncio.Event()
    agent_wait_finished = asyncio.Event()
    session = Session(_config(timeout_config=TimeoutConfig(agent_timeout=0.01)))

    async def _block_lifecycle(_event: BotStartedSpeaking) -> None:
        lifecycle_started.set()
        await release_lifecycle.wait()

    async def _observe_agent_wait(task: asyncio.Task[None], **_kwargs: object) -> None:
        await task
        agent_wait_finished.set()

    session.event_bus.subscribe(BotStartedSpeaking, _block_lifecycle)
    monkeypatch.setattr(turn_runner_module, "with_agent_timeout", _observe_agent_wait)
    session._turn = TurnContext("turn-lifecycle-timeout", CancelToken())

    run_task = asyncio.create_task(session._turn_runner.run_streaming_agent("hello", token=None))
    try:
        await asyncio.wait_for(lifecycle_started.wait(), timeout=0.5)
        await asyncio.wait_for(agent_wait_finished.wait(), timeout=0.5)
        assert not run_task.done()
    finally:
        release_lifecycle.set()

    await asyncio.wait_for(run_task, timeout=0.5)


async def test_first_synthesis_is_cancelled_and_drained_with_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_started = asyncio.Event()
    provider_finalized = asyncio.Event()
    never_release = asyncio.Event()
    provider_tasks: list[asyncio.Task[TTSSynthResult]] = []
    session = Session(_config())
    turn = TurnContext("turn-owned-first-synthesis", CancelToken())
    session._turn = turn
    state = _StreamingTtsState(
        turn=turn,
        identity=session._turn_runner._turn.capture_identity(),
        activity=session._turn_manager.capture_activity(),
        token=turn.cancel_token,
        queue=asyncio.Queue(),
    )
    state.queue.put_nowait(TTSInput("Reply."))

    async def _begin_synthesis(
        _payload: TTSInput,
        _token: CancelToken | None,
        *,
        is_active: object,
        lifecycle_ready: asyncio.Future[bool] | None = None,
        activity_started: object = None,
    ) -> asyncio.Task[TTSSynthResult]:
        _ = is_active, lifecycle_ready, activity_started

        async def _blocked_provider() -> TTSSynthResult:
            provider_started.set()
            try:
                await never_release.wait()
                return TTSSynthResult()
            finally:
                provider_finalized.set()

        task = asyncio.create_task(_blocked_provider())
        provider_tasks.append(task)
        return task

    monkeypatch.setattr(
        session._tts_scheduler,
        "begin_synthesis_with_bot_start",
        _begin_synthesis,
    )

    consumer = asyncio.create_task(session._turn_runner._synthesize_queued_payloads(state))
    await asyncio.wait_for(provider_started.wait(), timeout=0.5)
    consumer.cancel()

    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert provider_finalized.is_set()
    assert len(provider_tasks) == 1
    assert provider_tasks[0].cancelled()


@pytest.mark.asyncio
async def test_run_streaming_agent_emits_bot_stopped_after_drain() -> None:
    """``bot_stopped_speaking`` must be emitted after drain when not stopping."""
    stopped: list[BotStoppedSpeaking] = []
    order: list[str] = []
    session = Session(_config())
    session.event_bus.subscribe(AgentFinal, lambda _e: order.append("agent_final"))

    def _record_stopped(event: BotStoppedSpeaking) -> None:
        stopped.append(event)
        order.append("bot_stopped")

    session.event_bus.subscribe(BotStoppedSpeaking, _record_stopped)
    session.begin_turn("turn-stop2", CancelToken())
    await session._turn_runner.run_streaming_agent("hello", token=None)
    assert len(stopped) == 1
    assert order == ["agent_final", "bot_stopped"]


async def test_agent_phase_failure_releases_tts_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session(_config())
    turn = TurnContext("turn-agent-phase-failure", CancelToken())
    session._turn = turn
    captured_tts_task: asyncio.Task[None] | None = None

    async def _fail_lifecycle_wait(
        _state: _StreamingTtsState,
        tts_task: asyncio.Task[None],
    ) -> None:
        nonlocal captured_tts_task
        captured_tts_task = tts_task
        await asyncio.sleep(0)
        raise RuntimeError("lifecycle wait failed")

    monkeypatch.setattr(
        session._turn_runner,
        "_await_first_tts_lifecycle_ready",
        _fail_lifecycle_wait,
    )

    with pytest.raises(RuntimeError, match="lifecycle wait failed"):
        await session._turn_runner.run_streaming_agent("hello", token=None, turn=turn)

    assert captured_tts_task is not None
    await asyncio.wait_for(
        asyncio.gather(captured_tts_task, return_exceptions=True),
        timeout=0.5,
    )
    assert captured_tts_task.done()


async def test_agent_final_cancellation_drains_tts_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_handler_started = asyncio.Event()
    tts_consumer_started = asyncio.Event()
    tts_consumer_finalized = asyncio.Event()
    never_release = asyncio.Event()
    session = Session(_config())
    turn = TurnContext("turn-agent-final-cancel", CancelToken())
    session._turn = turn

    async def _blocked_tts_consumer(state: _StreamingTtsState) -> None:
        tts_consumer_started.set()
        state.first_tts_lifecycle_ready.set()
        try:
            await never_release.wait()
        finally:
            tts_consumer_finalized.set()

    async def _blocked_final_handler(_event: AgentFinal) -> None:
        final_handler_started.set()
        await never_release.wait()

    monkeypatch.setattr(
        session._turn_runner,
        "_consume_tts_payloads",
        _blocked_tts_consumer,
    )
    session.event_bus.subscribe(AgentFinal, _blocked_final_handler)

    run_task = asyncio.create_task(
        session._turn_runner.run_streaming_agent("hello", token=None, turn=turn)
    )
    await asyncio.wait_for(tts_consumer_started.wait(), timeout=0.5)
    await asyncio.wait_for(final_handler_started.wait(), timeout=0.5)
    run_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert tts_consumer_finalized.is_set()


@pytest.mark.asyncio
async def test_run_streaming_agent_records_total_latency_metric() -> None:
    journal = InMemoryRingBuffer(capacity=64)
    session = Session(
        _config(
            journal=journal,
        )
    )
    turn = TurnContext("turn-total-latency", CancelToken())
    turn.end_time = time.monotonic() - 1.0
    session._turn = turn

    await session._turn_runner.run_streaming_agent("hello", token=None, turn=turn)

    metric = next(record for record in journal.read() if record.name == "turn_total_latency_ms")
    assert metric.kind == JournalRecordKind.METRIC
    assert metric.turn_id == turn.id
    assert metric.data["from"] == "turn_ended"
    assert metric.data["to"] == "first_tts_audio"
    assert isinstance(metric.data["value"], float)
    # Latency is reported, not gated: no budget tags or alert records.
    assert "latency_budget_exceeded" not in metric.data
    assert not any(record.name == "latency_budget_exceeded" for record in journal.read())


@pytest.mark.asyncio
async def test_agent_timeout_emits_error() -> None:
    """Hitting the agent timeout emits an Error event."""

    class _SlowAgent(_TestBridgeBase):
        async def run(self, text: str) -> str:
            return ""

        async def invoke(
            self,
            turn_input: AgentTurnInput,
            recorder: AgentRecorder,
            cancel_token: CancelToken | None = None,
        ) -> AsyncIterator[AgentBridgeEvent]:
            _ = turn_input, recorder, cancel_token
            await asyncio.sleep(0.5)
            yield AgentBridgeEvent(kind="done", text="")

    session = Session(
        _config(
            agent=AgentRunner(_SlowAgent()),
            timeout_config=TimeoutConfig(agent_timeout=0.05),
        )
    )
    errors: list[Error] = []
    session.event_bus.subscribe(Error, lambda e: errors.append(e))

    session._turn = TurnContext("turn-timeout", CancelToken())
    await session._turn_runner.run_streaming_agent("hello", token=None)
    assert any(e.stage == ErrorStage.AGENT for e in errors)
