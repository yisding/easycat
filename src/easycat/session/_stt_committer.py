"""Owns STT segment commit scheduling for a Session.

A Session feeds audio to an STT provider; periodically (driven by
VAD pause events or end-of-speech), the committer asks the provider
to flush its buffered segment and resolves the resulting transcript
future. The committer is the single owner of:

- the "STT is currently consuming audio" flag (``_active``)
- the in-flight segment commit task
- the scheduled commit task (delayed after VAD pause)
- the background STT event consumer task

Session delegates to one ``STTCommitter`` instance per session.

End-stream sequencing contract
------------------------------

``TurnRunner.handle_end_of_speech`` calls :meth:`end_stream` between two
:meth:`await_pending` calls.  The first await blocks on segment commit;
``end_stream`` may generate one more segment; the second await blocks on
that.  Callers are responsible for preserving that ordering — the
committer's :meth:`end_stream` only enqueues a future when there is
uncommitted audio and then forwards the call to the provider.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from easycat._concurrency import RuntimeSupervisor, SurvivorCapacityError
from easycat.events import (
    Error,
    ErrorStage,
    EventBus,
    STTEventType,
    STTFinal,
    STTPartial,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.providers import PendingCommitReporter, STTProvider
from easycat.runtime.capabilities import close_if_supported
from easycat.runtime.scope import (
    RuntimeMemberPolicy,
    RuntimeScope,
    RuntimeTaskAction,
    RuntimeTaskPolicy,
)
from easycat.session._journal_sink import SessionJournalSink
from easycat.timeouts import STTTimeoutError, TimeoutConfig, resolve_provider_name
from easycat.turn_manager import TurnManagerState

if TYPE_CHECKING:
    from easycat._epoch import Lease
    from easycat._turn_context import TurnContext
    from easycat.session._wiring import SessionWiringContext
    from easycat.turn_manager import TurnManager

logger = logging.getLogger(__name__)

_PROVIDER_CLOSE_RETRY_INITIAL_S = 0.05
_PROVIDER_CLOSE_RETRY_MAX_S = 1.0
_PROVIDER_CLOSE_RETRY_ATTEMPTS = 3


def _pending_commit_bytes(provider: STTProvider) -> int | None:
    """Read a provider's uncommitted-audio byte count, if it exposes one.

    Uses the type-checkable :class:`~easycat.providers.PendingCommitReporter`
    surface; providers that do not implement it record ``None``.
    """
    if isinstance(provider, PendingCommitReporter):
        return provider.pending_commit_bytes()
    return None


class STTCommitter:
    """Schedules and commits STT segments for a Session."""

    FINAL_CLOSE_TASK_NAME = "stt_end_stream_after_final"
    SEGMENT_COMMIT_TASK_NAME = "stt_segment_commit"
    PROVIDER_END_TASK_NAME = "stt_provider_end_stream"
    PROVIDER_END_COHORT = "stt-provider-end-stream"
    PROVIDER_CLOSE_TASK_NAME = "stt_provider_close"
    PROVIDER_CLOSE_COHORT = "stt-provider-close"
    PROVIDER_ERROR_TASK_NAME = "stt_provider_error_notification"

    def __init__(
        self,
        *,
        wiring: SessionWiringContext,
        event_bus: EventBus,
        journal_sink: SessionJournalSink,
        runtime_scope: RuntimeScope,
        timeout_config: TimeoutConfig,
        segment_silence_ms: int,
        no_turn: TurnContext,
        turn_manager: TurnManager,
        on_speech_detection_reset: Callable[[], None] = lambda: None,
    ) -> None:
        self._stt_getter = wiring.stt
        self._event_bus = event_bus
        self._journal_sink = journal_sink
        self._runtime_scope = runtime_scope
        self._timeout_config = timeout_config
        self._segment_silence_ms = segment_silence_ms
        self._no_turn = no_turn
        self._current_turn = wiring.current_turn
        self._capture_identity = wiring.capture_identity
        self._turn_manager = turn_manager
        self._emit = wiring.emit
        self._auto_turn_from_stt_final = wiring.auto_turn_from_stt_final
        self._stt_track_label = wiring.stt_track_label
        self._on_speech_detection_reset = on_speech_detection_reset
        self._segment_commit_policy = RuntimeTaskPolicy(
            graceful=RuntimeMemberPolicy(
                cohort="stt-segment-commit",
                signal_token=False,
                task_action=RuntimeTaskAction.FINISH,
            ),
            force=RuntimeMemberPolicy(
                cohort="stt-segment-commit",
                signal_token=False,
                task_action=RuntimeTaskAction.CANCEL,
                hard_deadline=timeout_config.stt_timeout,
            ),
        )
        self._provider_end_policy = RuntimeTaskPolicy(
            graceful=RuntimeMemberPolicy(
                cohort=self.PROVIDER_END_COHORT,
                signal_token=False,
                task_action=RuntimeTaskAction.FINISH,
            ),
            force=RuntimeMemberPolicy(
                cohort=self.PROVIDER_END_COHORT,
                signal_token=False,
                task_action=RuntimeTaskAction.CANCEL,
                hard_deadline=0.0,
            ),
        )
        self._provider_close_policy = RuntimeTaskPolicy(
            graceful=RuntimeMemberPolicy(
                cohort=self.PROVIDER_CLOSE_COHORT,
                signal_token=False,
                task_action=RuntimeTaskAction.FINISH,
            ),
            force=RuntimeMemberPolicy(
                cohort=self.PROVIDER_CLOSE_COHORT,
                signal_token=False,
                task_action=RuntimeTaskAction.CANCEL,
                hard_deadline=0.0,
            ),
        )

        self._active: bool = False
        self._stt_task: asyncio.Task[None] | None = None
        self._pause_commit_task: asyncio.Task[None] | None = None
        self._segment_commit_task: asyncio.Task[None] | None = None
        self._pause_by_future: dict[asyncio.Future[str], Lease[None]] = {}
        self._stream_end_pending = False
        self._stream_end_completed = False
        self._provider_end_scope: RuntimeScope | None = None
        self._provider_end_scope_retired = False
        self._provider_end_scope_generation = 0
        self._provider_end_lock = asyncio.Lock()
        self._provider_close_transferred = False
        self._provider_close_pending = False
        self._provider_close_error: Exception | None = None
        self._provider_close_scope: RuntimeScope | None = None
        self._provider_close_scope_retired = False
        self._provider_close_scope_generation = 0
        self._provider_close_lock = asyncio.Lock()
        self._provider_error_supervisor = RuntimeSupervisor(capacity=1)
        self._provider_error_runtime_scope = RuntimeScope.create_root(
            name="stt-provider-errors",
            root_id=f"{runtime_scope.owner_id}:stt-provider-errors",
            supervisor=self._provider_error_supervisor,
            survivor_capacity=1,
        )
        self._provider_error_scope_generation = 0

    # ── Track labelling ───────────────────────────────────────────

    def _resolve_track(self, event_track: str | None) -> str | None:
        """Pick the track label to publish on STT events.

        A provider-supplied track always wins.  When the provider leaves it
        unset (the common case — most STT providers do not stamp a track), fall
        back to the transport's declared inbound label (e.g. Twilio's
        ``"inbound"``) so telephony classifiers receive the track they need.
        """
        if event_track is not None:
            return event_track
        return self._stt_track_label()

    # ── Active-flag accessors ─────────────────────────────────────

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def stt_task(self) -> asyncio.Task[None] | None:
        """The background STT event-consumer task, if one is running.

        Read-only handle used by teardown/diagnostics to confirm the
        consumer has been cancelled (or was never started).
        """
        return self._stt_task

    @property
    def requires_successor_handoff(self) -> bool:
        """Whether a successor turn must finish the prior STT lifecycle first.

        A PROCESSING-state barge-in can cancel ``on_turn_ended`` while it is
        still waiting for a delayed segment final.  At that point ``_active``
        has already been cleared, but the provider stream and its event
        consumer are still live.  Starting the successor stream directly would
        race (or be rejected by) the same provider's open stream.

        The committed-transcript fast path has the inverse shape: its event
        consumer may already have completed while the scoped close task is
        still releasing the provider.  Include both ownership signals.
        """
        event_task = self._stt_task
        return bool(
            self._active
            or (event_task is not None and not event_task.done())
            or any(
                not task.done() for task in self._runtime_scope.tasks(self.FINAL_CLOSE_TASK_NAME)
            )
            or self._stream_end_pending
            or bool(self._runtime_scope.tasks(self.PROVIDER_END_TASK_NAME))
            # ``schedule_turn_ended`` clears the cached task handle as soon
            # as it requests cancellation. A cancellation-resistant provider
            # commit remains owned by RuntimeScope, though, and can still hold
            # the stream open. Include that authoritative task ledger before
            # admitting a successor stream.
            # A completed commit remains a handoff gate until cancel() drains
            # its ledger entry and closes the provider stream. This matters
            # after a prior bounded handoff timed out and returned without
            # calling end_stream().
            or bool(self._runtime_scope.tasks(self.SEGMENT_COMMIT_TASK_NAME))
        )

    def mark_active(self) -> None:
        self._active = True
        self._stream_end_completed = False

    def begin_stream_attempt(self) -> None:
        """Invalidate prior end completion before provider startup can partially fail."""
        self._stream_end_completed = False

    def mark_inactive(self) -> None:
        self._active = False

    # ── Task handles ──────────────────────────────────────────────

    def clear_task_handles(self) -> None:
        """Clear cached task handles (used during shutdown drain)."""
        self._pause_commit_task = None
        self._segment_commit_task = None

    # ── Scheduling API ────────────────────────────────────────────

    def cancel_scheduled(
        self,
        _event: VADStartSpeaking | None = None,
        turn: TurnContext | None = None,
    ) -> None:
        task = self._pause_commit_task
        if task is not None and not task.done():
            task.cancel()
        self._pause_commit_task = None

    def cancel_inflight(self) -> None:
        task = self._segment_commit_task
        if task is not None and not task.done():
            task.cancel()
        self._segment_commit_task = None

    def resolve_pending(self, turn: TurnContext | None, value: str) -> None:
        if turn is None or turn is self._no_turn:
            return
        while turn.pending_stt_segment_futures:
            future = turn.pending_stt_segment_futures.pop(0)
            self._pause_by_future.pop(future, None)
            if not future.done():
                future.set_result(value)

    def schedule(
        self,
        _event: VADStopSpeaking,
        turn: TurnContext | None = None,
    ) -> None:
        """Finalize the current STT segment on a shorter pause than turn end."""
        if turn is None:
            turn = self._current_turn()
        if not self._active or turn is None or self._auto_turn_from_stt_final():
            return
        self.cancel_scheduled()
        delay_s = self._segment_silence_ms / 1000.0
        identity = self._capture_turn_identity(turn)
        pause = self._turn_manager.capture_pause()
        self._pause_commit_task = self._runtime_scope.create_journaled_task(
            self._commit_segment_after(
                delay_s,
                turn=turn,
                identity=identity,
                pause=pause,
            ),
            name="stt_pause_commit",
            journal_sink=self._journal_sink,
        )
        self._pause_commit_task.add_done_callback(self._runtime_scope.log_task_exception)

    async def _commit_segment_after(
        self,
        delay_s: float,
        turn: TurnContext | None,
        *,
        identity: Lease[TurnContext | None] | None = None,
        pause: Lease[None],
    ) -> None:
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        activity = self._turn_manager.capture_activity()
        if (
            not self._identity_is_current(identity, turn)
            or not self._activity_is_current(activity, TurnManagerState.USER_PAUSED)
            or not pause.guard()
        ):
            return
        await self._start_segment_commit(
            turn=turn,
            pause=pause,
            activity=activity,
            identity=identity,
        )

    async def _start_segment_commit(
        self,
        turn: TurnContext | None = None,
        *,
        pause: Lease[None] | None = None,
        activity: Lease[TurnManagerState] | None = None,
        identity: Lease[TurnContext | None] | None = None,
    ) -> None:
        if (
            turn is None
            or turn is self._no_turn
            or not self._identity_is_current(identity, turn)
            or turn.cancel_token.is_cancelled
            or not self._active
            or not turn.stt_has_uncommitted_audio
            or (pause is not None and not pause.guard())
            or (
                activity is not None
                and not self._activity_is_current(activity, TurnManagerState.USER_PAUSED)
            )
        ):
            return
        if self._segment_commit_task is not None and not self._segment_commit_task.done():
            return
        start_gate = asyncio.Event()

        async def _commit() -> None:
            try:
                await start_gate.wait()
                await self.commit_now(
                    turn=turn,
                    pause=pause,
                    activity=activity,
                    identity=identity,
                )
            finally:
                await self._finish_transferred_provider_close()

        task = await self._runtime_scope.start_owned_task(
            self.SEGMENT_COMMIT_TASK_NAME,
            _commit,
            policy=self._segment_commit_policy,
        )
        self._segment_commit_task = task
        try:
            resolved_turn = self._journal_sink.current_turn_id(turn.id)
            self._journal_sink.append_record(
                name="task_scheduled",
                turn_id=resolved_turn,
                data={"task_name": self.SEGMENT_COMMIT_TASK_NAME},
            )
        except BaseException:
            task.cancel()
            start_gate.set()
            task.add_done_callback(self._runtime_scope.log_task_exception)
            raise

        def _journal_terminal(completed: asyncio.Task[None]) -> None:
            if completed.cancelled():
                record_name = "task_cancelled"
                data = {"task_name": self.SEGMENT_COMMIT_TASK_NAME}
            else:
                try:
                    exc = completed.exception()
                except asyncio.CancelledError:
                    record_name = "task_cancelled"
                    data = {"task_name": self.SEGMENT_COMMIT_TASK_NAME}
                else:
                    record_name = "task_completed" if exc is None else "task_raised"
                    data = {"task_name": self.SEGMENT_COMMIT_TASK_NAME}
                    if exc is not None:
                        data["exc_type"] = type(exc).__name__
            self._journal_sink.append_record(
                name=record_name,
                turn_id=resolved_turn,
                data=data,
            )

        task.add_done_callback(_journal_terminal)
        task.add_done_callback(self._runtime_scope.log_task_exception)
        start_gate.set()

    async def commit_now(
        self,
        turn: TurnContext | None,
        *,
        pause: Lease[None] | None = None,
        activity: Lease[TurnManagerState] | None = None,
        identity: Lease[TurnContext | None] | None = None,
    ) -> None:
        owner_task = asyncio.current_task()
        commit_segment = getattr(self._stt_getter(), "commit_segment", None)
        if (
            turn is None
            or turn is self._no_turn
            or not self._identity_is_current(identity, turn)
            or not callable(commit_segment)
            or turn.cancel_token.is_cancelled
            or not turn.stt_has_uncommitted_audio
            or (pause is not None and not pause.guard())
            or (
                activity is not None
                and not self._activity_is_current(activity, TurnManagerState.USER_PAUSED)
            )
        ):
            return

        next_segment_index = len(turn.stt_segments) + 1
        # Pull the provider's pending-commit byte count (if exposed)
        # into the journal so bundles show *why* a commit was skipped
        # or accepted.  ``OpenAIRealtimeSTT`` tracks this precisely;
        # providers that cannot report it record None and the journal
        # reader treats it as unknown.
        pending_bytes = _pending_commit_bytes(self._stt_getter())
        self._journal_sink.append_record(
            name="stt_segment_commit_requested",
            turn_id=turn.id,
            data={
                "segment_index": next_segment_index,
                "transcript_text": turn.transcript_text,
                "pending_commit_bytes": (
                    int(pending_bytes) if isinstance(pending_bytes, int) else None
                ),
            },
        )
        turn.stt_has_uncommitted_audio = False
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        turn.pending_stt_segment_futures.append(future)
        if pause is not None:
            self._pause_by_future[future] = pause
        committed = False
        try:
            committed = await commit_segment()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("STT segment commit failed", exc_info=True)
        finally:
            self._journal_sink.append_record(
                name="stt_segment_commit_result",
                turn_id=turn.id,
                data={
                    "segment_index": next_segment_index,
                    "committed": committed,
                    "transcript_text": turn.transcript_text,
                },
            )
            if not committed:
                if (
                    self._identity_is_current(identity, turn)
                    and (
                        activity is None
                        or self._activity_is_current(activity, TurnManagerState.USER_PAUSED)
                    )
                    and (pause is None or pause.guard())
                ):
                    turn.stt_has_uncommitted_audio = True
                if future in turn.pending_stt_segment_futures:
                    turn.pending_stt_segment_futures.remove(future)
                self._pause_by_future.pop(future, None)
                if not future.done():
                    future.set_result("")
            if self._segment_commit_task is owner_task:
                self._segment_commit_task = None

    @staticmethod
    def _activity_is_current(
        activity: Lease[TurnManagerState],
        state: TurnManagerState,
    ) -> bool:
        """Whether a delayed segment commit still owns the pause activity."""
        return activity.value is state and activity.guard()

    def _capture_turn_identity(
        self,
        turn: TurnContext | None,
    ) -> Lease[TurnContext | None] | None:
        """Capture identity when ``turn`` comes from live Session wiring.

        Direct-construction compatibility harnesses historically pass detached
        turns while their wiring has no Session identity owner. Those calls
        retain their local-object semantics; production turns always match the
        live pointer and therefore carry an exact lease.
        """
        identity = self._capture_identity()
        if identity.value is turn:
            return identity
        return None

    @staticmethod
    def _identity_is_current(
        identity: Lease[TurnContext | None] | None,
        turn: TurnContext | None,
    ) -> bool:
        """Whether a Session-owned STT effect still belongs to ``turn``."""
        return identity is None or (identity.value is turn and identity.guard())

    async def await_pending(self, turn: TurnContext | None) -> bool:
        if turn is None or turn is self._no_turn:
            return True
        timeout = self._timeout_config.stt_timeout if self._timeout_config else None
        while turn.pending_stt_segment_futures:
            future = turn.pending_stt_segment_futures[0]
            try:
                if timeout:
                    await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
                else:
                    await future
            except TimeoutError:
                # ``wait_for`` (and therefore TimeoutError) is only used
                # when a timeout is configured.
                assert timeout is not None
                name = resolve_provider_name(self._stt_getter(), "stt")
                err = STTTimeoutError(name, timeout)
                await self._schedule_provider_operation_error(
                    Error(exception=err, stage=ErrorStage.STT, provider=name),
                    timeout=timeout,
                )
                return False
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Ignoring STT segment wait failure", exc_info=True)
            finally:
                if (
                    turn.pending_stt_segment_futures
                    and turn.pending_stt_segment_futures[0] is future
                ):
                    turn.pending_stt_segment_futures.pop(0)
                self._pause_by_future.pop(future, None)
        return True

    async def await_inflight_commit(self) -> None:
        """Await any in-flight ``commit_now`` task to completion."""
        task = self._segment_commit_task
        if task and not task.done():
            await task

    async def end_stream(self, turn: TurnContext | None) -> bool:
        """Finish the STT stream, enqueuing a future if uncommitted audio remains.

        ``TurnRunner.handle_end_of_speech`` calls this between two
        :meth:`await_pending` calls — the first await blocks on segment
        commit; ``end_stream`` may generate one more segment; the second
        await blocks on that.
        """
        if turn is not None and turn is not self._no_turn and turn.stt_has_uncommitted_audio:
            turn.stt_has_uncommitted_audio = False
            future = asyncio.get_running_loop().create_future()
            turn.pending_stt_segment_futures.append(future)
        return await self._finish_provider_end_stream(turn)

    async def _finish_provider_end_stream(
        self,
        turn: TurnContext | None,
        *,
        cancel_existing: bool = False,
    ) -> bool:
        """Bound one provider end-stream attempt while retaining unfinished work."""
        async with self._provider_end_lock:
            return await self._finish_provider_end_stream_locked(
                turn,
                cancel_existing=cancel_existing,
            )

    async def _finish_provider_end_stream_locked(
        self,
        turn: TurnContext | None,
        *,
        cancel_existing: bool,
    ) -> bool:
        """Serialize end-stream creation, bounded waiting, and retry state."""
        tasks = self._runtime_scope.tasks(self.PROVIDER_END_TASK_NAME)
        task = tasks[0] if tasks else None
        if task is not None and cancel_existing:
            task.cancel()
            settled = await self._await_owned_provider_operation(
                task,
                name=self.PROVIDER_END_TASK_NAME,
                cohort=self.PROVIDER_END_COHORT,
                turn=turn,
            )
            if settled or not task.done():
                return settled
            # A prior close accepted cancellation. Retry the provider's
            # retained cleanup obligation in a fresh owned task before a
            # successor stream is admitted.
            task = None
        if task is None and self._stream_end_completed:
            return True
        if task is None:
            self._stream_end_pending = True
            self._stream_end_completed = False

            async def _end_provider_stream() -> None:
                try:
                    await self._stt_getter().end_stream()
                    self._stream_end_pending = False
                    self._stream_end_completed = True
                finally:
                    await self._finish_transferred_provider_close()

            try:
                task = await self._provider_operation_scope(close=False).start_owned_task(
                    self.PROVIDER_END_TASK_NAME,
                    _end_provider_stream,
                    policy=self._provider_end_policy,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - provider ownership boundary
                provider = self._stt_getter()
                name = resolve_provider_name(provider, "stt")
                self.resolve_pending(turn, "")
                await self._schedule_provider_operation_error(
                    Error(exception=exc, stage=ErrorStage.STT, provider=name),
                    timeout=self._timeout_config.stt_timeout,
                )
                return False
            task.add_done_callback(self._runtime_scope.log_task_exception)

        return await self._await_owned_provider_operation(
            task,
            name=self.PROVIDER_END_TASK_NAME,
            cohort=self.PROVIDER_END_COHORT,
            turn=turn,
        )

    async def _await_owned_provider_operation(
        self,
        task: asyncio.Task[None],
        *,
        name: str,
        cohort: str,
        turn: TurnContext | None,
    ) -> bool:
        """Wait one configured interval, then cancel and park unfinished work."""
        timeout = self._timeout_config.stt_timeout
        done, _pending = await asyncio.wait({task}, timeout=timeout)
        if task in done:
            return await self._finish_owned_provider_operation(
                task,
                turn=turn,
                timeout=timeout,
            )
        return await self._handle_provider_operation_timeout(
            task,
            name=name,
            cohort=cohort,
            turn=turn,
            timeout=timeout,
        )

    async def _finish_owned_provider_operation(
        self,
        task: asyncio.Task[None],
        *,
        turn: TurnContext | None,
        timeout: float,
    ) -> bool:
        """Reap a terminal provider lifecycle task and report real failures."""
        self._runtime_scope.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            self.resolve_pending(turn, "")
            return False
        except Exception as exc:
            provider_name = resolve_provider_name(self._stt_getter(), "stt")
            logger.debug("STT provider lifecycle operation failed", exc_info=True)
            await self._schedule_provider_operation_error(
                Error(exception=exc, stage=ErrorStage.STT, provider=provider_name),
                timeout=timeout,
            )
            self.resolve_pending(turn, "")
            return False
        return True

    async def _handle_provider_operation_timeout(
        self,
        task: asyncio.Task[None],
        *,
        name: str,
        cohort: str,
        turn: TurnContext | None,
        timeout: float,
    ) -> bool:
        """Cancel a timed-out operation, retain survivors, and notify observers."""
        task.cancel()
        await asyncio.sleep(0)
        try:
            await self._runtime_scope.drain_cohort(cohort, force=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("STT provider operation hard-timeout drain failed", exc_info=True)
        self._retain_timed_out_provider_operation(task, name=name)
        provider = self._stt_getter()
        provider_name = resolve_provider_name(provider, "stt")
        err = STTTimeoutError(provider_name, timeout)
        await self._schedule_provider_operation_error(
            Error(exception=err, stage=ErrorStage.STT, provider=provider_name),
            timeout=timeout,
        )
        self.resolve_pending(turn, "")
        # A cooperatively-cancelled task may have been removed by drain_cohort;
        # the pending flag still gates a retry because cancellation does not
        # prove that provider finalization completed.
        if name == self.PROVIDER_CLOSE_TASK_NAME and task.done() and not task.cancelled():
            return not self._provider_close_pending
        return False

    def _retain_timed_out_provider_operation(
        self,
        task: asyncio.Task[None],
        *,
        name: str,
    ) -> None:
        """Retire the task's child scope until cancellation-resistant work settles."""
        if task.done():
            return
        if name == self.PROVIDER_END_TASK_NAME:
            self._provider_end_scope_retired = True
            scope = self._provider_end_scope
        elif name == self.PROVIDER_CLOSE_TASK_NAME:
            self._provider_close_scope_retired = True
            scope = self._provider_close_scope
        else:  # pragma: no cover - internal callers use the two constants
            scope = None
        if scope is not None:
            self._schedule_provider_scope_prune(scope, task)

    async def _schedule_provider_operation_error(
        self,
        event: Error,
        *,
        timeout: float,
    ) -> None:
        """Notify timeout observers without gating provider ownership settlement."""
        generation = self._provider_error_scope_generation
        self._provider_error_scope_generation += 1
        scope = self._provider_error_runtime_scope.create_child(f"notification-{generation}")
        cohort = f"stt-provider-error-{generation}"
        hard_deadline = max(timeout, 0.01)
        policy = RuntimeTaskPolicy(
            graceful=RuntimeMemberPolicy(
                cohort=cohort,
                signal_token=False,
                task_action=RuntimeTaskAction.FINISH,
                hard_deadline=hard_deadline,
            ),
            force=RuntimeMemberPolicy(
                cohort=cohort,
                signal_token=False,
                task_action=RuntimeTaskAction.CANCEL,
                hard_deadline=hard_deadline,
            ),
        )

        async def _notify() -> None:
            try:
                await self._emit(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("STT provider Error notification failed", exc_info=True)

        try:
            task = await scope.start_owned_task(
                self.PROVIDER_ERROR_TASK_NAME,
                _notify,
                policy=policy,
            )
        except SurvivorCapacityError:
            self._provider_error_runtime_scope.prune_empty_child(scope)
            logger.warning("STT provider Error notification skipped: survivor capacity full")
            return

        signal = scope.signal_cohort(cohort, force=False)

        async def _bound_notification() -> None:
            try:
                await scope.drain_cohort(signal)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("STT provider Error notification drain failed", exc_info=True)

        controller = self._provider_error_runtime_scope.create_task(
            f"{self.PROVIDER_ERROR_TASK_NAME}_controller",
            _bound_notification(),
        )

        def _prune_notification_scope(_completed: asyncio.Task[None]) -> None:
            asyncio.get_running_loop().call_soon(
                self._provider_error_runtime_scope.prune_empty_child,
                scope,
            )

        def _observe_controller(completed: asyncio.Task[None]) -> None:
            self._provider_error_runtime_scope.log_task_exception(completed)
            self._provider_error_runtime_scope.discard(completed)
            asyncio.get_running_loop().call_soon(
                self._provider_error_runtime_scope.prune_empty_child,
                scope,
            )

        task.add_done_callback(_prune_notification_scope)
        controller.add_done_callback(_observe_controller)
        # Let prompt synchronous/reserved observers run before returning the
        # provider ownership result, without joining arbitrary public awaits.
        await asyncio.sleep(0)

    # ── Background STT event consumer ─────────────────────────────

    def start_event_loop(
        self,
        turn: TurnContext | None = None,
        *,
        identity: Lease[TurnContext | None] | None = None,
    ) -> None:
        """Start background consumption of provider-scoped STT events."""
        if self._stt_task and not self._stt_task.done():
            self._stt_task.cancel()
        identity = identity or self._capture_turn_identity(turn)

        async def _consume() -> None:
            my_task = asyncio.current_task()
            try:
                async for stt_event in self._stt_getter().events():
                    if not self._identity_is_current(identity, turn):
                        break
                    if turn and turn.cancel_token.is_cancelled:
                        break
                    track = self._resolve_track(stt_event.track)
                    if stt_event.type == STTEventType.PARTIAL:
                        await self._emit(STTPartial(text=stt_event.text, track=track))
                    elif stt_event.type == STTEventType.FINAL:
                        pause = self._take_next_pause(turn)
                        if pause is not None:
                            self._turn_manager.on_stt_final(
                                stt_event.text,
                                pause=pause,
                            )
                        if turn and turn is not self._no_turn:
                            if not turn.pending_stt_segment_futures:
                                turn.stt_has_uncommitted_audio = False
                            turn.append_stt_segment(stt_event.text, track=track)
                            data: dict[str, Any] = {
                                "segment_index": len(turn.stt_segments),
                                "text": stt_event.text,
                                "track": track,
                                "transcript_text": turn.transcript_text,
                            }
                            # Provider-captured metadata reaches the journal —
                            # the single source of truth for observability —
                            # only when populated, so records stay lean for
                            # providers that don't report it.
                            if stt_event.confidence is not None:
                                data["confidence"] = stt_event.confidence
                            if stt_event.word_timestamps is not None:
                                data["word_timestamps"] = [
                                    {"word": w.word, "start": w.start, "end": w.end}
                                    for w in stt_event.word_timestamps
                                ]
                            self._journal_sink.append_record(
                                name="stt_segment_final",
                                turn_id=turn.id,
                                data=data,
                            )
                        await self._emit(STTFinal(text=stt_event.text, track=track))
                        if not self._identity_is_current(identity, turn):
                            break
                        if turn and turn is not self._no_turn and turn.pending_stt_segment_futures:
                            future = turn.pending_stt_segment_futures.pop(0)
                            if not future.done():
                                future.set_result(stt_event.text)
                        if self._auto_turn_from_stt_final() and stt_event.ends_turn:
                            await self._turn_manager.end_turn()
            except Exception as exc:
                logger.exception("STT event loop error")
                self.resolve_pending(turn, "")
                await self._schedule_provider_operation_error(
                    Error(exception=exc, stage=ErrorStage.STT),
                    timeout=self._timeout_config.stt_timeout,
                )
            finally:
                # A predecessor consumer canceled by ``start_event_loop()``
                # must not clear futures that the successor has already
                # enqueued for the new turn.  Only the current owner of
                # self._stt_task is allowed to touch the shared list here.
                if self._stt_task is my_task:
                    self.resolve_pending(turn, "")

        self._stt_task = self._runtime_scope.create_journaled_task(
            _consume(),
            name="stt_event_loop",
            journal_sink=self._journal_sink,
            turn_id=turn.id if turn is not None and turn is not self._no_turn else None,
        )
        self._stt_task.add_done_callback(self._runtime_scope.log_task_exception)

    def _take_next_pause(self, turn: TurnContext | None) -> Lease[None] | None:
        """Consume correlation for the next pending segment final, if present."""
        if turn is None or turn is self._no_turn or not turn.pending_stt_segment_futures:
            return None
        return self._pause_by_future.pop(turn.pending_stt_segment_futures[0], None)

    @property
    def provider_close_transferred(self) -> bool:
        """Whether owned STT work has accepted the provider-close obligation."""
        return self._provider_close_transferred

    def transfer_provider_close_to_owned_work(self) -> bool:
        """Move force-stop provider close behind a still-running owned operation."""
        owned = (
            *self._runtime_scope.tasks(self.SEGMENT_COMMIT_TASK_NAME),
            *self._runtime_scope.tasks(self.PROVIDER_END_TASK_NAME),
            *self._runtime_scope.tasks(self.PROVIDER_CLOSE_TASK_NAME),
        )
        if not any(not task.done() for task in owned):
            return False
        self._provider_close_transferred = True
        self._provider_close_pending = True
        return True

    def _provider_operation_scope(self, *, close: bool) -> RuntimeScope:
        """Return an open child owner, replacing one retired by survivor parking."""
        if close:
            scope = self._provider_close_scope
            if scope is None or self._provider_close_scope_retired:
                if scope is not None and scope.empty:
                    self._runtime_scope.prune_empty_child(scope)
                generation = self._provider_close_scope_generation
                self._provider_close_scope_generation += 1
                scope = self._runtime_scope.create_child(f"stt-provider-close-{generation}")
                self._provider_close_scope = scope
                self._provider_close_scope_retired = False
            return scope

        scope = self._provider_end_scope
        if scope is None or self._provider_end_scope_retired:
            if scope is not None and scope.empty:
                self._runtime_scope.prune_empty_child(scope)
            generation = self._provider_end_scope_generation
            self._provider_end_scope_generation += 1
            scope = self._runtime_scope.create_child(f"stt-provider-end-{generation}")
            self._provider_end_scope = scope
            self._provider_end_scope_retired = False
        return scope

    def _schedule_provider_scope_prune(
        self,
        scope: RuntimeScope,
        task: asyncio.Task[None],
    ) -> None:
        """Prune a retired operation owner after its parked task is discarded."""

        def _schedule(_completed: asyncio.Task[None]) -> None:
            task.get_loop().call_soon(self._prune_provider_operation_scope, scope)

        task.add_done_callback(_schedule)

    def _prune_provider_operation_scope(self, scope: RuntimeScope) -> None:
        """Unlink one settled retired scope and its closed-owner metadata."""
        if not scope.empty or not self._runtime_scope.prune_empty_child(scope):
            return
        if self._provider_end_scope is scope:
            self._provider_end_scope = None
            self._provider_end_scope_retired = False
        if self._provider_close_scope is scope:
            self._provider_close_scope = None
            self._provider_close_scope_retired = False

    async def _finish_transferred_provider_close(self) -> None:
        """Finish a force-transferred close inside the existing owned task."""
        if not self._provider_close_pending:
            return
        retry_delay = _PROVIDER_CLOSE_RETRY_INITIAL_S
        attempt = 0
        while self._provider_close_pending:
            async with self._provider_close_lock:
                if not self._provider_close_pending:
                    return
                try:
                    await close_if_supported(self._stt_getter())
                except asyncio.CancelledError:
                    # Do not detach the obligation from this owned task. A
                    # fresh cancellation during close is reserved for loop /
                    # process shutdown and must still be allowed to unwind.
                    raise
                except Exception as exc:
                    attempt += 1
                    self._provider_close_error = exc
                    if attempt >= _PROVIDER_CLOSE_RETRY_ATTEMPTS:
                        logger.warning(
                            "Deferred STT provider close failed after %d attempts; "
                            "retaining it for a later stop retry",
                            attempt,
                            exc_info=True,
                        )
                        return
                    should_warn = attempt == 1 or (attempt & (attempt - 1)) == 0
                    log = logger.warning if should_warn else logger.debug
                    log(
                        "Deferred STT provider close failed (attempt %d); retrying in %.2fs",
                        attempt,
                        retry_delay,
                        exc_info=True,
                    )
                else:
                    self._provider_close_pending = False
                    self._provider_close_error = None
                    return
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, _PROVIDER_CLOSE_RETRY_MAX_S)

    async def retry_transferred_provider_close(self) -> bool:
        """Bound a later retry after a transferred provider close failed."""
        if not self._provider_close_pending:
            return True
        if any(
            not task.done()
            for task in (
                *self._runtime_scope.tasks(self.SEGMENT_COMMIT_TASK_NAME),
                *self._runtime_scope.tasks(self.PROVIDER_END_TASK_NAME),
                *self._runtime_scope.tasks(self.PROVIDER_CLOSE_TASK_NAME),
            )
        ):
            return False

        async def _retry_close() -> None:
            await self._finish_transferred_provider_close()

        task = await self._provider_operation_scope(close=True).start_owned_task(
            self.PROVIDER_CLOSE_TASK_NAME,
            _retry_close,
            policy=self._provider_close_policy,
        )
        task.add_done_callback(self._runtime_scope.log_task_exception)
        settled = await self._await_owned_provider_operation(
            task,
            name=self.PROVIDER_CLOSE_TASK_NAME,
            cohort=self.PROVIDER_CLOSE_COHORT,
            turn=None,
        )
        return settled and not self._provider_close_pending

    # ── Cancellation ──────────────────────────────────────────────

    async def _cancel_segment_commit_handoff(
        self,
        turn: TurnContext | None,
    ) -> bool:
        """Bound cancellation cleanup that gates a successor STT stream."""
        timeout = self._timeout_config.stt_timeout if self._timeout_config else None
        try:
            if timeout:
                await asyncio.wait_for(
                    self._runtime_scope.cancel_and_drain(self.SEGMENT_COMMIT_TASK_NAME),
                    timeout=timeout,
                )
            else:
                await self._runtime_scope.cancel_and_drain(self.SEGMENT_COMMIT_TASK_NAME)
        except TimeoutError:
            assert timeout is not None
            self.resolve_pending(turn, "")
            provider = self._stt_getter()
            name = resolve_provider_name(provider, "stt")
            err = STTTimeoutError(name, timeout)
            logger.warning("STT segment commit cancellation timed out: %s", err)
            await self._schedule_provider_operation_error(
                Error(exception=err, stage=ErrorStage.STT, provider=name),
                timeout=timeout,
            )
            # Leave the provider call owned and abort this cancellation pass.
            # A normal successor publication is deferred; force teardown has
            # already signalled the task's force cohort and will park it at
            # that cohort's hard deadline.
            return False
        return True

    async def cancel(self, turn: TurnContext | None = None) -> bool:
        """Cancel all STT work; preserves the original ``_cancel_stt`` ordering."""
        await self._runtime_scope.cancel_and_drain("stt_pause_commit")
        segment_commit_drained = await self._cancel_segment_commit_handoff(turn)
        if not segment_commit_drained:
            self._active = False
            self._on_speech_detection_reset()
            await self._runtime_scope.cancel_and_drain("stt_event_loop")
            self._stt_task = None
            self.resolve_pending(turn, "")
            return False
        # The committed-transcript fast path may still be closing the same
        # provider in parallel with agent work. Drain it before invoking the
        # provider teardown below so a barge-in cannot issue concurrent closes
        # or let the old close race a successor stream.
        await self._runtime_scope.cancel_and_drain(self.FINAL_CLOSE_TASK_NAME)
        self._pause_commit_task = None
        self._segment_commit_task = None
        provider_end_drained = await self._finish_provider_end_stream(
            turn,
            cancel_existing=True,
        )
        self._active = False
        self._on_speech_detection_reset()
        await self._runtime_scope.cancel_and_drain("stt_event_loop")
        self._stt_task = None
        self.resolve_pending(turn, "")
        return provider_end_drained
