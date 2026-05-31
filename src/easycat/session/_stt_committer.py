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
from easycat.runtime.scope import RuntimeScope
from easycat.session._journal_sink import SessionJournalSink
from easycat.timeouts import STTTimeoutError, TimeoutConfig, resolve_provider_name
from easycat.turn_manager import TurnManagerState

if TYPE_CHECKING:
    from easycat._turn_context import TurnContext
    from easycat.session._wiring import SessionWiringContext
    from easycat.turn_manager import TurnManager

logger = logging.getLogger(__name__)


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
        self._turn_manager = turn_manager
        self._emit = wiring.emit
        self._auto_turn_from_stt_final = wiring.auto_turn_from_stt_final
        self._on_speech_detection_reset = on_speech_detection_reset

        self._active: bool = False
        self._stt_task: asyncio.Task[None] | None = None
        self._pause_commit_task: asyncio.Task[None] | None = None
        self._segment_commit_task: asyncio.Task[None] | None = None

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

    def mark_active(self) -> None:
        self._active = True

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
        self._pause_commit_task = self._runtime_scope.create_journaled_task(
            self._commit_segment_after(delay_s, turn=turn),
            name="stt_pause_commit",
            journal_sink=self._journal_sink,
        )
        self._pause_commit_task.add_done_callback(self._runtime_scope.log_task_exception)

    async def _commit_segment_after(self, delay_s: float, turn: TurnContext | None) -> None:
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        if self._turn_manager.state != TurnManagerState.USER_PAUSED:
            return
        await self._start_segment_commit(turn=turn)

    async def _start_segment_commit(self, turn: TurnContext | None = None) -> None:
        if (
            turn is None
            or turn is self._no_turn
            or turn.cancel_token.is_cancelled
            or not self._active
            or not turn.stt_has_uncommitted_audio
        ):
            return
        if self._segment_commit_task is not None and not self._segment_commit_task.done():
            return
        self._segment_commit_task = self._runtime_scope.create_journaled_task(
            self.commit_now(turn=turn),
            name="stt_segment_commit",
            journal_sink=self._journal_sink,
            turn_id=turn.id,
        )
        self._segment_commit_task.add_done_callback(self._runtime_scope.log_task_exception)

    async def commit_now(self, turn: TurnContext | None) -> None:
        commit_segment = getattr(self._stt_getter(), "commit_segment", None)
        if (
            turn is None
            or turn is self._no_turn
            or not callable(commit_segment)
            or turn.cancel_token.is_cancelled
            or not turn.stt_has_uncommitted_audio
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
        future = asyncio.get_running_loop().create_future()
        turn.pending_stt_segment_futures.append(future)
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
                turn.stt_has_uncommitted_audio = True
                if future in turn.pending_stt_segment_futures:
                    turn.pending_stt_segment_futures.remove(future)
                if not future.done():
                    future.set_result("")
            self._segment_commit_task = None

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
                name = resolve_provider_name(self._stt_getter(), "stt")
                err = STTTimeoutError(name, timeout)
                await self._emit(Error(exception=err, stage=ErrorStage.STT, provider=name))
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
        return True

    async def await_inflight_commit(self) -> None:
        """Await any in-flight ``commit_now`` task to completion."""
        task = self._segment_commit_task
        if task and not task.done():
            await task

    async def end_stream(self, turn: TurnContext | None) -> None:
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
        await self._stt_getter().end_stream()

    # ── Background STT event consumer ─────────────────────────────

    def start_event_loop(self, turn: TurnContext | None = None) -> None:
        """Start background consumption of provider-scoped STT events."""
        if self._stt_task and not self._stt_task.done():
            self._stt_task.cancel()

        async def _consume() -> None:
            my_task = asyncio.current_task()
            try:
                async for stt_event in self._stt_getter().events():
                    if turn and turn.cancel_token.is_cancelled:
                        break
                    if stt_event.type == STTEventType.PARTIAL:
                        await self._emit(STTPartial(text=stt_event.text, track=stt_event.track))
                    elif stt_event.type == STTEventType.FINAL:
                        if turn and turn is not self._no_turn:
                            if not turn.pending_stt_segment_futures:
                                turn.stt_has_uncommitted_audio = False
                            turn.append_stt_segment(stt_event.text, track=stt_event.track)
                            data: dict[str, Any] = {
                                "segment_index": len(turn.stt_segments),
                                "text": stt_event.text,
                                "track": stt_event.track,
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
                        await self._emit(STTFinal(text=stt_event.text, track=stt_event.track))
                        if turn and turn is not self._no_turn and turn.pending_stt_segment_futures:
                            future = turn.pending_stt_segment_futures.pop(0)
                            if not future.done():
                                future.set_result(stt_event.text)
                        if self._auto_turn_from_stt_final():
                            await self._turn_manager.end_turn()
            except Exception as exc:
                logger.exception("STT event loop error")
                await self._emit(Error(exception=exc, stage=ErrorStage.STT))
            finally:
                # A predecessor consumer canceled by ``start_event_loop()``
                # must not clear futures that the successor has already
                # enqueued for the new turn.  Only the current owner of
                # self._stt_task is allowed to touch the shared list here.
                if self._stt_task is my_task:
                    self.resolve_pending(turn, "")

        self._stt_task = asyncio.create_task(_consume())

    # ── Cancellation ──────────────────────────────────────────────

    async def cancel(self, turn: TurnContext | None = None) -> None:
        """Cancel all STT work; preserves the original ``_cancel_stt`` ordering."""
        await self._runtime_scope.cancel_and_drain("stt_pause_commit")
        await self._runtime_scope.cancel_and_drain("stt_segment_commit")
        self._pause_commit_task = None
        self._segment_commit_task = None
        try:
            await self._stt_getter().end_stream()
        except Exception:
            pass
        self._active = False
        self._on_speech_detection_reset()
        if self._stt_task and not self._stt_task.done():
            self._stt_task.cancel()
            try:
                await self._stt_task
            except (asyncio.CancelledError, Exception):
                pass
        self._stt_task = None
        self.resolve_pending(turn, "")
