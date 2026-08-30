"""Owns the per-turn agent loop for a Session.

Responsibilities:

- React to ``TurnStarted`` / ``TurnEnded`` events emitted by the
  ``TurnManager``.  Subscriptions are wired by ``Session.__init__``
  after the runner has been constructed.
- ``handle_end_of_speech``: drain pending STT segments, fetch the
  final transcript, dispatch to the agent.
- ``run_streaming_agent``: drive the agent stream through
  ``consume_agent_stream`` and synthesize TTS payloads sentence by
  sentence; track interruption; record the interruption notification
  at the end of the turn.
- ``send_text`` / ``_execute_text_turn``: same agent flow but with no
  audio pipeline.
- Coordinate with STTCommitter (drain pending segments),
  TTSScheduler (prepare and synthesize payloads), AudioRouter
  (drain outbound audio), CancelOrchestrator (signal propagation),
  and TurnManager (lifecycle state transitions).

TurnRunner is the hub. It depends on every other collaborator. The
constructor signature documents that explicitly — no surprises.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from easycat import _observability as observability
from easycat._epoch import Lease
from easycat._log_context import bind_turn, reset_turn
from easycat._tts_synthesizer import TTSSynthResult
from easycat._turn_context import TurnContext, TurnHandle
from easycat.cancel import CancelToken
from easycat.events import (
    AgentDelta,
    AgentFinal,
    AgentRequestStarted,
    Error,
    ErrorStage,
    EventBus,
    STTFinal,
    TurnEnded,
    TurnStarted,
    _is_turn_started_observation,
    _mark_turn_started_observation,
)
from easycat.integrations.agents._agent_runner import PreparedAgentResponse
from easycat.integrations.agents._text_stream import AgentTextStream
from easycat.integrations.agents.base import AgentBridgeEvent
from easycat.runtime.context import RunContext
from easycat.runtime.records import JournalRecordKind
from easycat.runtime.scope import RuntimeScope
from easycat.session._journal_sink import SessionJournalSink
from easycat.session._streaming import (
    AgentStreamResult,
    consume_agent_stream,
    emit_tool_event,
)
from easycat.session.interruption import (
    TtsChunk,
    estimate_and_notify_interruption,
)
from easycat.session.interruption import (
    notify_bridge_interruption as _notify_bridge_interruption,
)
from easycat.session.text import _text_for_estimation_timeline
from easycat.stages.agent import AgentStage
from easycat.strip_markdown import strip_markdown
from easycat.teardown_budgets import (
    SESSION_APPLICATION_PROMPT_CANCEL_DRAIN_TIMEOUT_S as _APPLICATION_PROMPT_CANCEL_DRAIN_S,
)
from easycat.teardown_budgets import (
    SESSION_STT_REJECTION_CLEANUP_CANCEL_GRACE_TIMEOUT_S as _STT_REJECTION_CLEANUP_GRACE_S,
)
from easycat.teardown_budgets import (
    SESSION_STT_REJECTION_CLEANUP_JOIN_TIMEOUT_S as _STT_REJECTION_CLEANUP_JOIN_S,
)
from easycat.timeouts import (
    AgentTimeoutError,
    TimeoutConfig,
    TTSTimeoutError,
    with_agent_timeout,
)
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManager, TurnManagerState, TurnPublication

if TYPE_CHECKING:
    from easycat.integrations.agents.base import AgentBridgeEvent
    from easycat.session._audio_router import AudioRouter
    from easycat.session._cancel_orchestrator import CancelOrchestrator
    from easycat.session._stt_committer import STTCommitter
    from easycat.session._tts_scheduler import TTSScheduler
    from easycat.session._wiring import SessionWiringContext
    from easycat.stages.stt import STTStage

logger = logging.getLogger(__name__)
_APPLICATION_SYSTEM_TRIGGER = "Follow the application instruction above."


def _new_first_tts_payload_gate() -> asyncio.Future[bool]:
    """Create the per-turn first-payload admission future on the active loop."""
    return asyncio.get_running_loop().create_future()


@dataclass
class _StreamingTtsState:
    """Mutable per-turn TTS state shared between the streaming phases.

    Replaces the closure locals that ``run_streaming_agent`` used to share
    with its nested ``_process_tts`` consumer.
    """

    turn: TurnContext
    identity: Lease[TurnContext | None]
    activity: Lease[TurnManagerState]
    token: CancelToken | None
    queue: asyncio.Queue[TTSInput | None]
    #: Released after first-payload lifecycle dispatch (or a no-audio terminal
    #: path) so AgentFinal cannot overtake BotStartedSpeaking.
    first_tts_lifecycle_ready: asyncio.Event = field(default_factory=asyncio.Event)
    #: Resolves after the AgentDelta dispatch that admitted the first TTS
    #: payload. False rejects speculative provider work after dispatch failure.
    first_tts_payload_ready: asyncio.Future[bool] = field(
        default_factory=_new_first_tts_payload_gate
    )
    #: Released after the outer task has emitted (or intentionally skipped)
    #: AgentFinal so fast TTS completion cannot overtake agent output ordering.
    agent_output_settled: asyncio.Event = field(default_factory=asyncio.Event)
    #: Set before externally cancelling the agent consumer after a provider
    #: failure/timeout so its finalizer drops incomplete buffered text.
    agent_stream_aborted: asyncio.Event = field(default_factory=asyncio.Event)
    #: The consumer received its first payload and decided gating/playback.
    synth_started: bool = False
    #: Gate state snapshotted at first-payload time (see ``_settle_turn_after_tts``).
    gated: bool = False
    playback_started: bool = False
    # True only if playback was cut off mid-stream by a cancelled token
    # (a genuine barge-in), as opposed to the queue draining naturally.
    # A turn that finishes speaking and *then* has its token cancelled by a
    # later turn must not be retro-truncated as "interrupted during
    # playback".
    playback_cut_short: bool = False
    should_stop: bool = False
    chunks: list[TtsChunk] = field(default_factory=list)
    error: Exception | None = None


@dataclass(frozen=True)
class _PreemptiveAgentResult:
    """Terminal result of a history-isolated preemptive agent attempt."""

    response: PreparedAgentResponse | None = None
    error: Exception | None = None


@dataclass
class _TextTurnStreamState:
    accumulated: str = ""
    text_stream: AgentTextStream = field(default_factory=AgentTextStream)
    structured_output: object | None = None
    pending_tool_calls: Counter[str | None] = field(default_factory=Counter)


class TurnRunner:
    """Drives the per-turn agent loop."""

    _TEXT_TURN_TASK_NAME = "text_turn"
    _APPLICATION_PROMPT_TASK_NAME = "application_prompt"
    _PREEMPTIVE_TASK_NAME = "preemptive_agent_generation"

    def __init__(
        self,
        *,
        wiring: SessionWiringContext,
        stt_committer: STTCommitter,
        tts_scheduler: TTSScheduler,
        audio_router: AudioRouter,
        cancel_orchestrator: CancelOrchestrator,
        turn_manager: TurnManager,
        agent_stage: AgentStage,
        run_ctx: RunContext,
        event_bus: EventBus,
        journal_sink: SessionJournalSink,
        runtime_scope: RuntimeScope,
        timeout_config: TimeoutConfig,
        on_agent_failure: str | Callable[[Exception], str] | None,
        turn_handle: TurnHandle,
        stt_stage: STTStage,
        session_id: str,
        journal_enabled: bool,
    ) -> None:
        self._stt = stt_committer
        self._tts = tts_scheduler
        self._audio = audio_router
        self._cancel = cancel_orchestrator
        self._turn_manager = turn_manager
        self._agent_stage = agent_stage
        self._run_ctx = run_ctx
        self._event_bus = event_bus
        self._journal_sink = journal_sink
        self._runtime_scope = runtime_scope
        self._timeout_config = timeout_config
        self._on_agent_failure = on_agent_failure
        self._turn = turn_handle
        self._stt_stage = stt_stage
        self._stt_provider = wiring.stt
        self._is_running = wiring.is_running
        self._is_gated = wiring.is_gated
        self._agent = wiring.agent
        self._drain_session_actions = wiring.drain_session_actions
        self._caller_id_system_message = wiring.caller_id_system_message
        self._cancel_turn = wiring.cancel_turn
        self._cut_off_tts_for_text_replacement = wiring.cut_off_tts_for_text_replacement
        self._stop = wiring.stop
        self._reset_turn_state = wiring.reset_turn_state
        self._emit = wiring.emit
        self._session_id = session_id
        self._journal_enabled = journal_enabled

        # Active text-turn tracking.
        self._active_text_turn: asyncio.Task[str] | None = None
        self._text_turn_cancel_token: CancelToken | None = None
        self._text_turn_accumulated: str = ""
        self._agent_turn_lock = asyncio.Lock()
        self._active_application_prompt: asyncio.Task[str] | None = None
        self._application_prompt_cancel_token: CancelToken | None = None
        self._application_turn_ids: set[str] = set()

        # Voice-only speculative generation. The task may run while the turn
        # manager is confirming an endpoint, but its result is not committed
        # to agent history until ``handle_end_of_speech`` confirms that the
        # transcript still matches.
        self._preemptive_task: asyncio.Task[_PreemptiveAgentResult] | None = None
        self._preemptive_transcript = ""
        self._preemptive_identity: Lease[TurnContext | None] | None = None
        self._preemptive_attempts = 0
        # Highest turn generation whose end-of-speech take point has passed.
        # A trailing STTFinal for such a turn (e.g. a provider flushing a
        # second final segment during the ``end_stream`` drain) must never
        # spawn new speculative work: the confirmed run for that turn is
        # already starting, and simple agents must never see overlapping
        # ``run()`` calls.
        self._preemptive_finalized_generation = 0

    # ── Introspection helpers (kept for Session shutdown paths) ──

    @property
    def active_text_turn(self) -> asyncio.Task[str] | None:
        return self._active_text_turn

    @property
    def text_turn_cancel_token(self) -> CancelToken | None:
        return self._text_turn_cancel_token

    @property
    def active_application_prompt(self) -> asyncio.Task[str] | None:
        return self._active_application_prompt

    @property
    def application_prompt_cancel_token(self) -> CancelToken | None:
        return self._application_prompt_cancel_token

    # ── Subscription handlers ─────────────────────────────────────

    async def on_turn_started(self, event: TurnStarted) -> None:
        """Route unmarked hand-built events through private lifecycle publication."""
        if _is_turn_started_observation(event) or event.turn_id in self._application_turn_ids:
            return
        turn_id = event.turn_id or f"turn-{uuid4().hex[:8]}"
        await self.on_turn_publication(
            TurnPublication(
                source="hand_built",
                session_id=event.session_id,
                turn_id=turn_id,
                cancel_token=self._turn_manager.cancel_token or CancelToken(),
                activity=self._turn_manager.capture_activity(),
            )
        )

    async def on_turn_publication(
        self,
        publication: TurnPublication,
    ) -> TurnPublication:
        """Install private voice lifecycle state before public TurnStarted observation."""
        if publication.source not in {"voice", "hand_built"}:
            return publication
        if not self._is_running():
            return replace(publication, admission_rejected=True)
        if not await self._prepare_turn_publication(publication):
            return replace(publication, admission_rejected=True)

        cancel_token = publication.cancel_token or CancelToken()
        turn = self._turn.begin(publication.turn_id, cancel_token)
        publication = replace(publication, identity=self._turn.capture_identity())
        self._preemptive_identity = publication.identity
        self._preemptive_attempts = 0
        # Tag startup records for this turn without leaving the EventBus task
        # pinned to the turn after this handler returns.
        turn_token = bind_turn(turn.id)
        startup_cleanup_joined = False
        try:
            self._audio.reset_speech_detection()
            self._tts.set_playback_suppressed(False)

            # Start STT stream
            stt = self._stt_provider()
            self._stt.begin_stream_attempt()
            await stt.start_stream()
            if not self._publication_owns_turn(publication, turn):
                _, cancellation = await self._cleanup_rejected_stt_start(publication, turn)
                startup_cleanup_joined = True
                self._raise_rejected_start_cancellation(publication, cancellation)
                return replace(publication, admission_rejected=True)
            self._stt.mark_active()

            # Prime STT with pre-roll frames captured by TurnManager.
            # The background event consumer is started only after the stream
            # is open and pre-roll priming succeeds, so a failure here cannot
            # leave an orphaned consumer task running against a half-open
            # stream for the rest of the session.
            for chunk in self._turn_manager.turn_audio:
                await self._stt_stage.execute(chunk, self._run_ctx, turn)
                if not self._publication_owns_turn(publication, turn):
                    _, cancellation = await self._cleanup_rejected_stt_start(publication, turn)
                    startup_cleanup_joined = True
                    self._raise_rejected_start_cancellation(publication, cancellation)
                    return replace(publication, admission_rejected=True)
                turn.stt_has_uncommitted_audio = True

            self._stt.start_event_loop(turn, identity=publication.identity)
        except asyncio.CancelledError:
            if not startup_cleanup_joined:
                await self._cleanup_rejected_stt_start(publication, turn)
            # A cleanup timeout deliberately retains Session identity as the
            # provider survivor's lifecycle gate. The cancelled TurnManager
            # callback will never receive an admission_rejected return value,
            # though, so roll back its activity/token here while this exact
            # publication still owns them. A newer publication makes the
            # lease stale and is therefore preserved.
            self._rollback_rejected_start_activity(publication)
            raise
        except Exception as exc:
            logger.exception("Failed to start STT stream")
            # Full per-turn teardown: close the (possibly half-open) stream,
            # cancel/await any STT consumer task, mark inactive, and resolve
            # pending futures so no live STT work or stale turn is left behind.
            _, cancellation = await self._cleanup_rejected_stt_start(publication, turn)
            self._raise_rejected_start_cancellation(
                publication,
                cancellation,
                cause=exc,
            )
            # Cleanup and owned-state rollback must precede fallible public
            # notification. Cancellation or a strict Error subscriber may
            # propagate from emit(), but can no longer strand the partial
            # provider stream or manager publication behind it.
            await self._emit(Error(exception=exc, stage=ErrorStage.STT))
            return replace(publication, admission_rejected=True)
        finally:
            reset_turn(turn_token)
        return publication

    async def _cleanup_rejected_stt_start(
        self,
        publication: TurnPublication,
        turn: TurnContext,
    ) -> tuple[bool, asyncio.CancelledError | None]:
        """Join bounded partial-start cleanup before rejecting publication.

        The caller may itself be cancelled while provider cleanup is running.
        Keep the cleanup task joined through settlement, then re-raise that
        external cancellation — but only within the teardown budget: a
        provider whose cleanup never settles must not make the caller (and
        transitively ``stop(force=True)``) uncancellable. Reset shared turn
        state only while this exact publication still owns both Session
        identity and manager activity, so stale cleanup cannot roll back a
        newer owner.
        """
        cleanup = asyncio.create_task(
            self._stt.cancel(turn),
            name=f"stt-start-rejection-cleanup-{turn.id}",
        )
        cleanup.add_done_callback(self._runtime_scope.log_task_exception)
        cancellation: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as exc:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    cancellation = cancellation or exc
                    break
                continue
            except Exception:  # noqa: BLE001 - inspect cleanup task result below
                break

        if cancellation is not None and not cleanup.done():
            cancellation = await self._join_cancelled_stt_cleanup(cleanup, cancellation)

        cleanup_complete = self._settled_stt_cleanup_result(cleanup)
        if cleanup_complete and self._publication_owns_turn(publication, turn):
            self._reset_turn_state()
        return cleanup_complete, cancellation

    @staticmethod
    async def _join_cancelled_stt_cleanup(
        cleanup: asyncio.Task[bool],
        cancellation: asyncio.CancelledError,
    ) -> asyncio.CancelledError:
        """Join a rejected-start cleanup within the teardown budget only."""
        try:
            done, _ = await asyncio.wait(
                {cleanup},
                timeout=_STT_REJECTION_CLEANUP_JOIN_S,
            )
            if not done:
                cleanup.cancel()
                await asyncio.wait({cleanup}, timeout=_STT_REJECTION_CLEANUP_GRACE_S)
        except asyncio.CancelledError:
            if not cleanup.done():
                cleanup.cancel()
        return cancellation

    @staticmethod
    def _settled_stt_cleanup_result(cleanup: asyncio.Task[bool]) -> bool:
        """Read a settled cleanup result; an unsettled task stays observed."""
        if not cleanup.done():
            logger.warning(
                "STT teardown after rejected start exceeded its cancellation join "
                "budget; cleanup task %s remains observed in the background",
                cleanup.get_name(),
            )
            return False
        try:
            return cleanup.result()
        except asyncio.CancelledError:
            logger.debug("STT teardown after rejected start was cancelled")
        except Exception:
            logger.debug("STT teardown after rejected start raised", exc_info=True)
        return False

    def _rollback_rejected_start_activity(self, publication: TurnPublication) -> None:
        """Reset only the manager activity/token still owned by this publication."""
        if publication.activity is not None and publication.activity.guard():
            self._turn_manager.reset()

    def _raise_rejected_start_cancellation(
        self,
        publication: TurnPublication,
        cancellation: asyncio.CancelledError | None,
        *,
        cause: BaseException | None = None,
    ) -> None:
        """Roll back manager ownership before propagating captured cancellation."""
        if cancellation is None:
            return
        self._rollback_rejected_start_activity(publication)
        if cause is not None:
            raise cancellation from cause
        raise cancellation

    async def _prepare_turn_publication(self, publication: TurnPublication) -> bool:
        """Drain predecessor ownership and re-guard this admission request."""
        await self.cancel_preemptive_generation()
        if publication.activity is None or not publication.activity.guard():
            return False

        # Cancel the previous turn's token so any in-flight agent/TTS work
        # notices the cancellation before we overwrite the turn pointer.
        prev = self._turn.current
        if prev and not prev.cancel_token.is_cancelled:
            prev.cancel_token.cancel()

        if self._stt.requires_successor_handoff:
            # Fast barge-in returns immediately after the audible cutoff and
            # deliberately leaves provider cleanup detached. If the previous
            # turn was still waiting for an STT final, the same provider cannot
            # admit a successor stream until that old lifecycle has closed.
            if (await self._stt.cancel(prev)) is False:
                return False
        else:
            self._stt.cancel_scheduled()
            self._stt.cancel_inflight()
            self._stt.resolve_pending(prev, "")

        # The handoff can suspend while a newer publication acquires manager
        # ownership. The original request must not install over that successor.
        return publication.activity.guard()

    def _publication_owns_turn(
        self,
        publication: TurnPublication,
        turn: TurnContext,
    ) -> bool:
        """Whether private publication work still owns identity and activity."""
        return bool(
            publication.identity is not None
            and self._identity_owns_turn(publication.identity, turn)
            and publication.activity is not None
            and publication.activity.guard()
        )

    async def on_stt_final(self, event: STTFinal) -> None:
        """Start history-isolated agent work while endpointing is still pending."""
        candidate = self._preemptive_candidate(event)
        if candidate is None:
            return
        identity, turn, transcript = candidate
        if self._preemptive_matches(turn, transcript):
            return

        # A later final segment invalidates the previous transcript. Cancel
        # and drain it before invoking the same simple agent again so agents
        # never see overlapping ``run()`` calls.
        await self.cancel_preemptive_generation()
        # The drain above can suspend; ``handle_end_of_speech`` may reach the
        # turn's take point meanwhile. Re-check before starting a fresh
        # attempt that could overlap the confirmed run.
        if self._preemptive_take_passed(turn):
            return
        identity = self._turn.capture_identity()
        if not self._identity_owns_turn(identity, turn):
            return
        if self._preemptive_identity is None or not self._identity_owns_turn(
            self._preemptive_identity, turn
        ):
            self._preemptive_identity = identity
            self._preemptive_attempts = 0
        if self._preemptive_attempts >= self._agent_stage.preemptive_max_retries:
            return

        self._preemptive_attempts += 1
        self._preemptive_transcript = transcript
        self._preemptive_identity = identity

        self._preemptive_task = self._runtime_scope.create_journaled_task(
            self._prepare_preemptive_response(identity, turn, transcript),
            name=self._PREEMPTIVE_TASK_NAME,
            journal_sink=self._journal_sink,
            turn_id=turn.id,
        )

    async def _prepare_preemptive_response(
        self,
        identity: Lease[TurnContext | None],
        turn: TurnContext,
        transcript: str,
    ) -> _PreemptiveAgentResult:
        """Invoke speculative provider work only before this turn's take point."""
        try:
            if not self._identity_owns_turn(identity, turn) or self._preemptive_take_passed(turn):
                return _PreemptiveAgentResult()
            response = await self._agent_stage.prepare_preemptive(transcript, turn)
            return _PreemptiveAgentResult(response=response)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            return _PreemptiveAgentResult(error=exc)

    def _preemptive_candidate(
        self,
        event: STTFinal,
    ) -> tuple[Lease[TurnContext | None], TurnContext, str] | None:
        """Return the active turn/transcript when speculative work is safe."""
        if not self._agent_stage.supports_preemptive_generation:
            return None
        identity = self._turn.capture_identity()
        turn = identity.value
        if not identity.guard() or turn is None or turn.cancel_token.is_cancelled:
            return None
        if self._preemptive_take_passed(turn):
            return None
        if event.turn_id is not None and event.turn_id != turn.id:
            return None

        transcript = turn.transcript_text
        if not transcript:
            return None
        return identity, turn, transcript

    def _preemptive_matches(self, turn: TurnContext, transcript: str) -> bool:
        """Whether the active attempt already targets this exact transcript."""
        return bool(
            self._preemptive_task is not None
            and self._preemptive_identity is not None
            and self._identity_owns_turn(self._preemptive_identity, turn)
            and self._preemptive_transcript == transcript
        )

    def _preemptive_take_passed(self, turn: TurnContext) -> bool:
        """Whether this turn is already past its end-of-speech take point.

        Turn generations increase monotonically, so any generation at or
        below the recorded finalized generation belongs to a turn whose
        prepared response was already taken (or discarded) by
        ``handle_end_of_speech``.
        """
        return turn.generation <= self._preemptive_finalized_generation

    def schedule_turn_ended(self, event: TurnEnded) -> None:
        """Schedule end-of-turn processing without blocking other handlers.

        Cancels BOTH the scheduled pause-commit task and any in-flight
        segment-commit task before running ``on_turn_ended``.  The in-flight
        cancel guards against the commit race that surfaced as OpenAI
        Realtime "buffer too small" errors on plan-7.
        """
        if event.turn_id in self._application_turn_ids:
            return
        self._stt.cancel_scheduled()
        self._stt.cancel_inflight()
        current_tts_task = self._tts.active_turn_task
        if current_tts_task and not current_tts_task.done():
            current_tts_task.cancel()
            # Keep old task alive until it completes to avoid concurrent runs (gh 1024)
        identity = self._turn.capture_identity()
        activity = self._turn_manager.capture_activity()
        turn_token = bind_turn(event.turn_id)
        try:
            turn_ended = self.on_turn_ended(event, identity, activity)
            if self._journal_enabled:
                new_task = self._runtime_scope.create_journaled_task(
                    turn_ended,
                    name="on_turn_ended",
                    journal_sink=self._journal_sink,
                    turn_id=event.turn_id,
                )
            else:
                new_task = self._runtime_scope.create_task("on_turn_ended", turn_ended)
        finally:
            reset_turn(turn_token)
        self._tts.active_turn_task = new_task
        new_task.add_done_callback(self._runtime_scope.log_task_exception)

    async def on_turn_ended(
        self,
        event: TurnEnded,
        identity: Lease[TurnContext | None],
        activity: Lease[TurnManagerState],
    ) -> None:
        """Handle TurnEnded from TurnManager: finalize STT and run agent/TTS."""
        turn_token = bind_turn(event.turn_id)
        try:
            if not identity.guard() or not self._activity_is_current(
                activity, TurnManagerState.PROCESSING
            ):
                return
            turn = identity.value
            if turn and turn.cancel_token.is_cancelled:
                return
            if turn:
                turn.end_time = event.timestamp
            await self.handle_end_of_speech(
                turn=turn,
                identity=identity,
                activity=activity,
            )
        finally:
            reset_turn(turn_token)

    # ── Pipeline ───────────────────────────────────────────────────

    async def handle_end_of_speech(
        self,
        turn: TurnContext | None = None,
        *,
        identity: Lease[TurnContext | None] | None = None,
        activity: Lease[TurnManagerState] | None = None,
    ) -> None:
        """Finalize STT, run the agent, synthesize TTS.

        ``turn`` defaults to the active session turn for backwards
        compatibility; internal callers always pass it explicitly.
        """
        identity = identity or self._turn.capture_identity()
        activity = activity or self._turn_manager.capture_activity()
        if turn is None:
            turn = identity.value
        token = turn.cancel_token if turn else None
        if turn is not None:
            # This turn is now past its take point: a trailing STTFinal (a
            # provider can flush a second final segment during the
            # ``end_stream`` drain below) must not start new speculation
            # that would overlap the confirmed ``run()`` for this turn.
            self._preemptive_finalized_generation = max(
                self._preemptive_finalized_generation, turn.generation
            )

        transcript, stt_close_task = self._take_committed_transcript(turn)
        if not transcript:
            transcript = await self._finalize_turn_transcript(
                turn,
                identity=identity,
                activity=activity,
            )

        if not transcript or (token and token.is_cancelled):
            await self.cancel_preemptive_generation()
            if self._identity_owns_turn(identity, turn) and activity.guard():
                self._reset_turn_state()
            if stt_close_task is not None:
                await asyncio.shield(stt_close_task)
            return

        try:
            await self._emit(
                AgentRequestStarted(
                    session_id=self._session_id,
                    turn_id=turn.id if turn is not None else None,
                )
            )
            prepared_response = await self._take_preemptive_response(transcript, turn)
            # The await above spans the remaining model latency. Speech may resume
            # during it, cancelling/replacing this turn. Never fall through to the
            # confirmed invocation for an abandoned transcript: even a cancelled
            # AgentRunner records its user message before it observes the token.
            if not self._is_active_voice_turn(turn, token, identity, activity):
                return
            await self.run_streaming_agent(
                transcript,
                token,
                turn=turn,
                prepared_response=prepared_response,
                identity=identity,
                activity=activity,
            )
        finally:
            if stt_close_task is not None:
                await asyncio.shield(stt_close_task)

    def _take_committed_transcript(
        self,
        turn: TurnContext | None,
    ) -> tuple[str, asyncio.Task[bool] | None]:
        """Start closing STT without delaying an already-final transcript.

        A final STT event clears ``stt_has_uncommitted_audio`` only after the
        provider has accounted for every submitted frame. With no pending
        commit future, that transcript is ready for the agent; the provider's
        stream can close concurrently with the much slower agent request.

        Keep journaled sessions on the sequential path so their task timeline
        and replay ordering remain unchanged.
        """
        self._stt.cancel_scheduled()
        if (
            self._journal_enabled
            or turn is None
            or turn.stt_has_uncommitted_audio
            or turn.pending_stt_segment_futures
        ):
            return "", None
        transcript = turn.transcript_text
        if not transcript:
            return "", None

        stt_needs_close = self._stt.is_active
        self._stt.mark_inactive()
        if not stt_needs_close:
            return transcript, None
        close_task = self._runtime_scope.create_task(
            self._stt.FINAL_CLOSE_TASK_NAME,
            self._stt.end_stream(turn),
        )
        return transcript, close_task

    def _is_active_voice_turn(
        self,
        turn: TurnContext | None,
        token: CancelToken | None,
        identity: Lease[TurnContext | None],
        activity: Lease[TurnManagerState],
    ) -> bool:
        """Whether a post-await voice turn still owns its captured identity."""
        return bool(
            turn is not None
            and not (token and token.is_cancelled)
            and self._identity_owns_turn(identity, turn)
            and activity.guard()
        )

    @staticmethod
    def _identity_owns_turn(
        identity: Lease[TurnContext | None],
        turn: TurnContext | None,
    ) -> bool:
        """Guard one atomically captured identity against its turn payload."""
        return identity.value is turn and identity.guard()

    @staticmethod
    def _activity_is_current(
        activity: Lease[TurnManagerState],
        state: TurnManagerState,
    ) -> bool:
        """Guard one atomically captured manager activity and expected state."""
        return activity.value is state and activity.guard()

    async def cancel_preemptive_generation(self) -> None:
        """Cancel and drain the current preemptive task, if any."""
        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling():
            # Checkpoint before mutating ownership. Otherwise a cancellation
            # requested immediately before entry is included in the baseline
            # below and mistaken for the speculative task's cancellation.
            await asyncio.sleep(0)
        task = self._preemptive_task
        self._preemptive_task = None
        self._preemptive_transcript = ""
        if task is None:
            return
        if task is asyncio.current_task():
            # A provider callback may request Session.stop() from inside
            # speculative generation. The callback owns the current stack, so
            # it cannot cancel/await itself; detach it and let the caller's
            # teardown close the surrounding session.
            self._runtime_scope.discard(cast(asyncio.Task[Any], task))
            return
        cancellation_requests = current_task.cancelling() if current_task is not None else 0
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # ``await task`` raises CancelledError both when the drained
            # speculative task was cancelled (expected — swallow it) and when
            # the *calling* task was itself cancelled during the drain window.
            # Re-raise the latter so a cancelled host (STT event consumer,
            # ``on_turn_ended``, ``Session.stop``) does not resume past its
            # cancellation point and keep working on a stale turn.
            if current_task is not None and current_task.cancelling() > cancellation_requests:
                raise
        finally:
            self._runtime_scope.discard(task)

    async def _take_preemptive_response(
        self,
        transcript: str,
        turn: TurnContext | None,
    ) -> PreparedAgentResponse | None:
        """Return a matching prepared response, otherwise discard it safely."""
        task = self._preemptive_task
        if (
            task is None
            or turn is None
            or self._preemptive_identity is None
            or not self._identity_owns_turn(self._preemptive_identity, turn)
            or self._preemptive_transcript != transcript
        ):
            await self.cancel_preemptive_generation()
            return None

        self._preemptive_task = None
        self._preemptive_transcript = ""
        try:
            if self._timeout_config and self._timeout_config.agent_timeout:
                result = await with_agent_timeout(
                    task,
                    timeout=self._timeout_config.agent_timeout,
                    event_bus=self._event_bus,
                )
            else:
                result = await task
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise
            return None
        except AgentTimeoutError:
            logger.debug("Preemptive agent generation timed out; using confirmed path")
            return None
        finally:
            self._runtime_scope.discard(task)
        if result.error is not None:
            logger.debug(
                "Preemptive agent generation failed; using confirmed path: %s",
                result.error,
            )
            return None
        return result.response

    async def _finalize_turn_transcript(
        self,
        turn: TurnContext | None,
        *,
        identity: Lease[TurnContext | None],
        activity: Lease[TurnManagerState],
    ) -> str:
        """Stop STT input, drain pending commits, and return the final transcript.

        Returns ``""`` when a pending STT future resolved empty/cancelled —
        the caller resets the turn state in that case.
        """
        self._stt.cancel_scheduled()

        # Stop forwarding audio to STT immediately so trailing frames
        # from continuous transports don't leak into the transcript.
        stt_needs_close = self._stt.is_active
        self._stt.mark_inactive()

        await self._stt.await_inflight_commit()
        if not self._is_active_voice_turn(turn, token=None, identity=identity, activity=activity):
            return ""

        pending_ready = await self._stt.await_pending(turn)
        if not self._is_active_voice_turn(turn, token=None, identity=identity, activity=activity):
            return ""
        if not pending_ready:
            # A pending-final timeout leaves the provider stream and its event
            # consumer live. Close both before the caller resets the turn, or
            # a successor can start a second stream against the same provider.
            await self._stt.cancel(turn)
            return ""

        if stt_needs_close and not await self._end_stt_stream_if_owned(
            turn,
            identity=identity,
            activity=activity,
        ):
            return ""

        pending_ready = await self._stt.await_pending(turn)
        if not self._is_active_voice_turn(turn, token=None, identity=identity, activity=activity):
            return ""
        if not pending_ready:
            await self._stt.cancel(turn)
            return ""

        transcript = ""
        if turn is not None:
            transcript = turn.transcript_text

        if (
            transcript
            and turn
            and self._is_active_voice_turn(turn, token=None, identity=identity, activity=activity)
        ):
            turn.stt_final_time = time.monotonic()
        return transcript

    async def _end_stt_stream_if_owned(
        self,
        turn: TurnContext | None,
        *,
        identity: Lease[TurnContext | None],
        activity: Lease[TurnManagerState],
    ) -> bool:
        """Close STT and confirm the same voice turn still owns publication."""
        if (await self._stt.end_stream(turn)) is False:
            return False
        return self._is_active_voice_turn(
            turn,
            token=None,
            identity=identity,
            activity=activity,
        )

    def _reset_turn_manager_preserving_token(self) -> None:
        """Reset the TurnManager to IDLE without cancelling the live turn token.

        ``TurnManager.reset()`` cancels its active ``cancel_token`` before
        clearing it (the documented teardown semantics shared with barge-in).
        The gated-replay keep-alive path needs the manager returned to IDLE
        with its buffers cleared, but the *current* turn's token must stay
        live — the concurrently-running agent stream and the buffered gated
        replay still depend on it.  Detach the manager's token reference
        first so ``reset()`` has nothing to cancel; the turn retains its own
        token via ``turn.cancel_token`` / the captured ``token`` local.
        """
        self._turn_manager.reset(preserve_token=True)

    # ── Streaming agent path ───────────────────────────────────────

    async def run_streaming_agent(
        self,
        transcript: str,
        token: CancelToken | None,
        *,
        turn: TurnContext | None = None,
        identity: Lease[TurnContext | None] | None = None,
        activity: Lease[TurnManagerState] | None = None,
        prepared_response: PreparedAgentResponse | None = None,
        system_prefix_override: str | None = None,
        input_role: Literal["system", "user"] = "user",
    ) -> str:
        """Streaming agent path with incremental TTS on sentence boundaries.

        Uses :func:`consume_agent_stream` to translate agent events into
        TTS payloads, and runs TTS synthesis concurrently.  The phases are
        named methods: ``_consume_tts_payloads`` (the TTS consumer task),
        ``_await_agent_task`` (agent timeout/cancellation handling),
        ``_finalize_streamed_text`` (final markdown strip), and
        ``_record_streaming_interruption`` (barge-in accounting).
        """
        identity = identity or self._turn.capture_identity()
        activity = activity or self._turn_manager.capture_activity()
        if turn is None:
            turn = identity.value
        assert turn is not None
        if not self._identity_owns_turn(identity, turn) or not activity.guard():
            return ""
        st = _StreamingTtsState(
            turn=turn,
            identity=identity,
            activity=activity,
            token=token,
            # Bounded so a fast agent against a slow/stalled TTS consumer
            # applies natural backpressure via consume_agent_stream's awaited
            # put(), matching the BoundedAudioQueue convention used elsewhere
            # in the pipeline.  The payloads are lightweight per-sentence
            # text, so 64 is ample headroom; the producer is bounded by
            # agent_timeout and runs inside the cancellable agent_task, so a
            # full queue cannot deadlock.
            queue=asyncio.Queue(maxsize=64),
        )

        agent_result: AgentStreamResult | None = None
        accumulated_text = ""
        system_prefix = (
            system_prefix_override
            if system_prefix_override is not None
            else self._caller_id_system_message()
        )

        async def _run_agent_consumer() -> None:
            nonlocal agent_result
            agent_result = await consume_agent_stream(
                stream_factory=lambda: self._agent_stage.execute_streaming(
                    transcript,
                    self._run_ctx,
                    turn,
                    cancel_token=token,
                    system_prefix=system_prefix,
                    prepared_response=prepared_response,
                    input_role=input_role,
                    commit_guard=lambda: self._streaming_turn_is_current(st),
                ),
                cancel_token=token,
                tts_queue=st.queue,
                emit=self._emit,
                prepare_tts_payload=self._tts.prepare,
                strip_md=self._tts.strip_markdown_enabled,
                turn=turn,
                first_tts_payload_ready=st.first_tts_payload_ready,
                abort_event=st.agent_stream_aborted,
                is_active=lambda: self._streaming_turn_is_current(st),
                on_tts_replacement_conflict=self._cut_off_tts_for_text_replacement,
            )

        agent_task = asyncio.create_task(_run_agent_consumer())
        tts_task = asyncio.create_task(self._consume_tts_payloads(st))

        try:
            try:
                caught_exc = await self._await_agent_task_recording_cancel(
                    st, agent_task, tts_task
                )
                # Lifecycle ordering is not agent execution time. Wait outside
                # ``_await_agent_task`` so slow BotStartedSpeaking handlers cannot trip
                # the agent timeout after the agent has already completed.
                await self._await_first_tts_lifecycle_ready(st, tts_task)
                agent_error = caught_exc or (agent_result.error if agent_result else None)
                interrupted = agent_result.interrupted if agent_result else False
                accumulated_text = agent_result.text if agent_result else ""
                structured_output = agent_result.structured_output if agent_result else None
                stream_succeeded = agent_error is None and self._streaming_turn_is_current(st)

                if self._tts.strip_markdown_enabled and accumulated_text and stream_succeeded:
                    accumulated_text = self._finalize_streamed_text(turn, accumulated_text)

                if (accumulated_text or structured_output is not None) and stream_succeeded:
                    await self._emit(
                        AgentFinal(text=accumulated_text, structured_output=structured_output)
                    )
                await self._maybe_speak_agent_failure_fallback(st, agent_error)
            finally:
                st.agent_output_settled.set()

            await self._await_tts_task_recording_cancel(st, tts_task)
        except BaseException:
            # Cancellation or a strict event-handler failure can land after
            # the agent wait but before the guarded TTS wait. Keep both spawned
            # tasks owned across that gap so provider work cannot outlive the
            # turn and leak stale audio into a later one.
            await self._cancel_and_drain(agent_task, tts_task)
            raise

        if agent_error is not None and st.error is not None:
            await self._emit(
                Error(
                    exception=ExceptionGroup(
                        "streaming turn failed",
                        [agent_error, st.error],
                    ),
                    stage=ErrorStage.PIPELINE,
                    turn_id=turn.id,
                )
            )

        self._record_streaming_interruption(st, interrupted=interrupted)

        if st.should_stop:
            await self._stop()
            return accumulated_text

        self._record_voice_total_latency(turn)

        # If a newer turn started (e.g. barge-in), avoid clobbering its state.
        if (
            self._identity_owns_turn(st.identity, turn)
            and st.activity.guard()
            and st.activity.value is not TurnManagerState.IDLE
        ):
            self._reset_turn_state()
        return accumulated_text

    # ── Streaming agent phases ─────────────────────────────────────

    async def _maybe_speak_agent_failure_fallback(
        self,
        st: _StreamingTtsState,
        error: Exception | None,
    ) -> None:
        if (
            error is None
            or st.error is not None
            or st.turn.audio_bytes_sent > 0
            or st.synth_started
            or not st.queue.empty()
            or (st.token and st.token.is_cancelled)
            or self._tts.is_playback_suppressed
            or not self._identity_owns_turn(st.identity, st.turn)
            or not self._activity_is_current(st.activity, TurnManagerState.PROCESSING)
        ):
            return
        await self._speak_agent_failure_fallback(st, error)

    def _resolve_agent_failure_fallback(self, error: Exception) -> str | None:
        policy = self._on_agent_failure
        if policy is None:
            return None
        try:
            text = policy(error) if callable(policy) else policy
        except Exception:
            logger.warning("on_agent_failure callback raised; fallback skipped", exc_info=True)
            return None
        if not isinstance(text, str) or not text.strip():
            logger.warning("on_agent_failure must resolve to non-empty text; fallback skipped")
            return None
        return text.strip()

    async def _speak_agent_failure_fallback(
        self,
        st: _StreamingTtsState,
        error: Exception,
    ) -> None:
        text = self._resolve_agent_failure_fallback(error)
        if text is None:
            return
        self._journal_sink.append_record(
            name="agent_failure_fallback",
            turn_id=st.turn.id,
            data={
                "text": text,
                "error_type": type(error).__name__,
            },
        )
        try:
            payload = self._tts.prepare(text, is_streaming=False, is_final=True)
            try:
                result = await self._synthesize_first_payload(st, payload)
            finally:
                st.synth_started = True
            assert result is not None
            st.chunks.append(
                TtsChunk(
                    _text_for_estimation_timeline(payload),
                    result.audio_bytes,
                    result.completed,
                )
            )
            if result.first_audio_time is not None and self._streaming_turn_is_current(st):
                st.turn.first_tts_audio_time = result.first_audio_time
            if self._streaming_turn_is_current(st):
                await self._emit(AgentFinal(text=text))
        except asyncio.CancelledError:
            raise
        except TTSTimeoutError as fallback_error:
            st.error = fallback_error
            logger.exception("Agent failure fallback TTS timed out")
        except Exception as fallback_error:
            st.error = fallback_error
            logger.exception("Agent failure fallback TTS failed")
            await self._emit(
                Error(
                    exception=fallback_error,
                    stage=ErrorStage.TTS,
                    turn_id=st.turn.id,
                )
            )

    async def _consume_tts_payloads(self, st: _StreamingTtsState) -> None:
        """TTS consumer task: synthesize queued payloads, then settle the turn."""
        cancelled = False
        try:
            await self._synthesize_queued_payloads(st)
        except asyncio.CancelledError:
            cancelled = True
        except TTSTimeoutError:
            await self._tts.cancel()
        except Exception as exc:
            st.error = exc
            logger.exception("TTS streaming error")
            await self._emit(Error(exception=exc, stage=ErrorStage.TTS))

        # Safety release for cancellation/error paths that exit before the
        # first queue item can make the more precise release below.
        st.first_tts_lifecycle_ready.set()

        # Decide whether playback was cut short by a barge-in *now* — while
        # still inside the consumer task and before ``finalize_speaking_turn``
        # emits bot_stopped_speaking (after which the next turn can start and
        # cancel this turn's now-superseded token). ``is_cancelled`` here
        # therefore reflects a cancellation observed *during* this turn's
        # playback, not a later turn retroactively cancelling the token.
        st.playback_cut_short = bool(st.token and st.token.is_cancelled)

        while not st.queue.empty():
            remaining = st.queue.get_nowait()
            if remaining is not None:
                st.chunks.append(TtsChunk(_text_for_estimation_timeline(remaining), 0, False))

        if cancelled:
            raise asyncio.CancelledError
        await self._settle_turn_after_tts(st)

    async def _synthesize_queued_payloads(self, st: _StreamingTtsState) -> None:
        """Drain the payload queue through the synthesizer until the sentinel."""
        while True:
            payload = await st.queue.get()
            if payload is None:
                st.first_tts_lifecycle_ready.set()
                break
            if not self._streaming_turn_is_current(st):
                st.first_tts_lifecycle_ready.set()
                st.chunks.append(TtsChunk(_text_for_estimation_timeline(payload), 0, False))
                break
            if st.token and st.token.is_cancelled:
                st.first_tts_lifecycle_ready.set()
                st.chunks.append(TtsChunk(_text_for_estimation_timeline(payload), 0, False))
                break
            if self._tts.is_playback_suppressed:
                st.first_tts_lifecycle_ready.set()
                st.chunks.append(TtsChunk(_text_for_estimation_timeline(payload), 0, False))
                break

            if not st.synth_started:
                # Snapshot the gate state at first-payload time and reuse it in
                # ``_settle_turn_after_tts``.  ``_is_gated`` is time-varying
                # (the classification gate can flush mid-synthesis); re-reading
                # it live later would tear down the turn pointer the gated
                # replay still needs for mark accounting.
                try:
                    result = await self._synthesize_first_payload(
                        st,
                        payload,
                        lifecycle_ready=st.first_tts_payload_ready,
                        lifecycle_started=st.first_tts_lifecycle_ready,
                    )
                finally:
                    st.synth_started = True
                    st.first_tts_lifecycle_ready.set()
                if result is None:
                    st.chunks.append(TtsChunk(_text_for_estimation_timeline(payload), 0, False))
                    break
            else:
                result = await self._tts.synthesizer.synthesize(
                    payload,
                    st.token,
                    is_active=(
                        None
                        if self._is_gated()
                        else lambda: (
                            not self._tts.is_playback_suppressed
                            and self._identity_owns_turn(st.identity, st.turn)
                            and self._activity_is_current(
                                st.activity, TurnManagerState.BOT_SPEAKING
                            )
                        )
                    ),
                )
            st.chunks.append(
                TtsChunk(
                    _text_for_estimation_timeline(payload),
                    result.audio_bytes,
                    result.completed,
                )
            )
            if (
                result.first_audio_time is not None
                and st.turn.first_tts_audio_time is None
                and self._streaming_turn_is_current(st)
            ):
                st.turn.first_tts_audio_time = result.first_audio_time

    async def _synthesize_first_payload(
        self,
        st: _StreamingTtsState,
        payload: TTSInput,
        *,
        lifecycle_ready: asyncio.Future[bool] | None = None,
        lifecycle_started: asyncio.Event | None = None,
    ) -> TTSSynthResult | None:
        """Admit the first payload through the shared classification gate."""
        st.gated = self._is_gated()
        if not self._streaming_turn_is_current(st):
            return None
        if st.gated:
            if lifecycle_ready is not None and not await asyncio.shield(lifecycle_ready):
                return None
            if not self._streaming_turn_is_current(st):
                return None
            return await self._tts.synthesizer.synthesize(
                payload,
                st.token,
                is_active=None,
            )

        task = await self._tts.begin_synthesis_with_bot_start(
            payload,
            st.token,
            is_active=lambda: (
                not self._tts.is_playback_suppressed and self._streaming_turn_is_current(st)
            ),
            lifecycle_ready=lifecycle_ready,
            activity_started=lambda activity: setattr(st, "activity", activity),
        )
        if lifecycle_started is not None:
            lifecycle_started.set()
        st.playback_started = self._activity_is_current(st.activity, TurnManagerState.BOT_SPEAKING)
        return await self._await_owned_first_synthesis(task)

    def _streaming_turn_is_current(self, st: _StreamingTtsState) -> bool:
        """Re-guard streaming identity and activity at one commit boundary."""
        return self._identity_owns_turn(st.identity, st.turn) and st.activity.guard()

    @staticmethod
    async def _await_owned_first_synthesis(
        task: asyncio.Task[TTSSynthResult],
    ) -> TTSSynthResult:
        """Propagate consumer cancellation and drain provider cleanup."""
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _settle_turn_after_tts(self, st: _StreamingTtsState) -> None:
        """Return the TurnManager toward IDLE (or keep the gated turn alive)."""
        current_task = asyncio.current_task()
        if current_task is None or current_task.cancelling() == 0:
            await st.agent_output_settled.wait()
        # Cancellation can unwind this old consumer while barge-in has already
        # installed a successor turn. Never finalize or reset shared turn state
        # on behalf of a stale generation.
        if not self._identity_owns_turn(st.identity, st.turn) or not st.activity.guard():
            return
        if st.synth_started and self._activity_is_current(
            st.activity, TurnManagerState.BOT_SPEAKING
        ):
            st.should_stop = await self._tts.finalize_speaking_turn(
                st.turn,
                identity=st.identity,
                activity=st.activity,
            )
        elif st.synth_started and not st.playback_started:
            if st.gated:
                # Keep current turn alive for gated replay mark accounting.
                # ``TurnManager.reset()`` cancels the active token before
                # dropping it (so barge-in/idle teardown cooperatively stops
                # bound work).  Here we deliberately want the *opposite*: the
                # turn's token (still in use by the concurrently-running agent
                # stream and the gated replay) must survive the manager reset,
                # or the agent turn is killed mid-flight and never emits its
                # AgentFinal.  ``reset(preserve_token=True)`` leaves the token
                # uncancelled; the turn keeps its own token.
                self._audio.reset_speech_detection()
                self._reset_turn_manager_preserving_token()
            else:
                self._reset_turn_state()

    async def _await_agent_task_recording_cancel(
        self,
        st: _StreamingTtsState,
        agent_task: asyncio.Task[None],
        tts_task: asyncio.Task[None],
    ) -> Exception | None:
        try:
            return await self._await_agent_task(st, agent_task, tts_task)
        except asyncio.CancelledError:
            self._record_streaming_interruption(
                st,
                interrupted=st.turn.last_barge_in_time is not None,
                source="streaming_turn_cancelled",
            )
            raise

    async def _await_tts_task_recording_cancel(
        self,
        st: _StreamingTtsState,
        tts_task: asyncio.Task[None],
    ) -> None:
        """Await final TTS without letting its cancellation consume ours."""
        try:
            await asyncio.shield(tts_task)
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is None or current_task.cancelling() == 0:
                # The TTS task was cancelled independently. Preserve the
                # historical behavior of treating that as a settled consumer.
                return

            await self._cancel_tts_for_streaming_turn(st, tts_task)
            raise

    async def _await_first_tts_lifecycle_ready(
        self,
        st: _StreamingTtsState,
        tts_task: asyncio.Task[None],
    ) -> None:
        """Preserve event order without charging lifecycle work to the agent."""
        ready_task = asyncio.create_task(st.first_tts_lifecycle_ready.wait())
        try:
            done, _pending = await asyncio.wait(
                (ready_task, tts_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ready_task in done:
                await ready_task
        except asyncio.CancelledError:
            await self._cancel_tts_for_streaming_turn(st, tts_task)
            raise
        finally:
            if not ready_task.done():
                ready_task.cancel()
                await asyncio.gather(ready_task, return_exceptions=True)

    async def _cancel_tts_for_streaming_turn(
        self,
        st: _StreamingTtsState,
        tts_task: asyncio.Task[None],
    ) -> None:
        await self._cancel_and_drain(tts_task)
        self._record_streaming_interruption(
            st,
            interrupted=st.turn.last_barge_in_time is not None,
            source="streaming_turn_cancelled",
        )

    async def _await_agent_task(
        self,
        st: _StreamingTtsState,
        agent_task: asyncio.Task[None],
        tts_task: asyncio.Task[None],
    ) -> Exception | None:
        """Await the agent under its timeout while preserving TTS settlement."""
        try:
            if self._timeout_config and self._timeout_config.agent_timeout:
                await with_agent_timeout(
                    asyncio.shield(agent_task),
                    timeout=self._timeout_config.agent_timeout,
                )
            else:
                await asyncio.shield(agent_task)
        except asyncio.CancelledError:
            await self._cancel_and_drain(agent_task, tts_task)
            raise
        except Exception as exc:
            # The TTS consumer owns turn settlement and may already be waiting
            # for ``agent_output_settled`` after receiving its sentinel. Keep
            # that ownership alive so a configured failure fallback can be
            # admitted before the consumer finalizes or resets the turn.
            st.agent_stream_aborted.set()
            await self._cancel_and_drain(agent_task)
            if isinstance(exc, AgentTimeoutError):
                # Drain the shielded agent before dispatching handlers so its
                # cleanup is complete before the turn can advance.
                await self._emit(Error(exception=exc, stage=ErrorStage.AGENT))
            else:
                logger.exception("Streaming agent error")
                await self._emit(Error(exception=exc, stage=ErrorStage.AGENT))
            return exc
        return None

    @staticmethod
    async def _cancel_and_drain(*tasks: asyncio.Task[None]) -> None:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _finalize_streamed_text(self, turn: TurnContext, accumulated_text: str) -> str:
        """Apply the final markdown strip and sync the agent framework state."""
        original_text = accumulated_text
        stripped = strip_markdown(accumulated_text, normalize_code_spans=True)
        self._tts._record_markdown_strip(
            phase="streaming_final",
            original_text=original_text,
            stripped_text=stripped,
            turn_id=turn.id,
        )
        if stripped != original_text:
            # Route through the stage so the framework-state rewrite lands
            # on the journal recording boundary alongside the streamed text.
            self._agent_stage.replace_last_assistant_text(
                stripped, ctx=self._run_ctx, turn_id=turn.id
            )
            return stripped
        return accumulated_text

    def _record_streaming_interruption(
        self,
        st: _StreamingTtsState,
        *,
        interrupted: bool,
        source: str = "streaming_turn",
    ) -> None:
        """Estimate what the caller heard and record a barge-in, if any."""
        interruption_notification = estimate_and_notify_interruption(
            self._agent_stage,
            st.token,
            st.turn,
            st.chunks,
            tts_playback_started=st.playback_started,
            tts_playback_cut_short=st.playback_cut_short,
            interrupted=interrupted,
            interruption_mode=self._cancel.interruption_mode,
            latency_compensation_ms=self._cancel.latency_compensation_ms,
            ack_stale_ms=self._cancel.ack_stale_ms,
            ack_tail_cap_ms=self._cancel.ack_tail_cap_ms,
            ctx=self._run_ctx,
        )
        if interruption_notification is not None:
            self._cancel.record_interruption(
                source=source,
                mode=interruption_notification.mode,
                text_spoken=interruption_notification.text_spoken,
                notified=interruption_notification.notified,
                turn_id=st.turn.id,
            )

    def _record_voice_total_latency(self, turn: TurnContext) -> None:
        """Record the turn's total latency from speech end to first audio."""
        if not self._journal_enabled or turn.end_time is None or turn.first_tts_audio_time is None:
            return
        elapsed_ms = max(0.0, (turn.first_tts_audio_time - turn.end_time) * 1000.0)
        self._journal_sink.append_record(
            kind=JournalRecordKind.METRIC,
            name="turn_total_latency_ms",
            turn_id=turn.id,
            data={
                "value": elapsed_ms,
                "from": "turn_ended",
                "to": "first_tts_audio",
            },
        )

    # ── Text mode ──────────────────────────────────────────────────

    async def _cancel_active_text_turn(self, *, source: str) -> None:
        previous = self._active_text_turn
        if previous is None or previous.done():
            return
        delivered = self._text_turn_accumulated
        if self._text_turn_cancel_token:
            self._text_turn_cancel_token.cancel()
        previous.cancel()
        await asyncio.gather(previous, return_exceptions=True)
        self._runtime_scope.discard(previous)
        notified = _notify_bridge_interruption(
            self._agent_stage,
            delivered,
            self._cancel.interruption_mode,
            ctx=self._run_ctx,
        )
        self._cancel.record_interruption(
            source=source,
            mode=self._cancel.interruption_mode,
            text_spoken=delivered,
            notified=notified,
        )

    async def cancel_application_prompt(
        self,
        *,
        drain_timeout_s: float = _APPLICATION_PROMPT_CANCEL_DRAIN_S,
    ) -> bool:
        """Signal prompt cancellation and wait only briefly for cleanup.

        Returns whether the prompt finished within the bound. Cancellation-
        resistant provider cleanup remains owned by its public prompt caller,
        but cannot stall barge-in, reset, or force stop.
        """
        previous = self._active_application_prompt
        if previous is None or previous.done():
            return True
        if self._application_prompt_cancel_token is not None:
            self._application_prompt_cancel_token.cancel()
        if previous is asyncio.current_task():
            return False
        previous.cancel()
        done, _ = await asyncio.wait({previous}, timeout=drain_timeout_s)
        if previous in done:
            self._runtime_scope.discard(previous)
            return True
        self._runtime_scope.discard(previous)
        return False

    async def send_text(self, text: str, *, admit: Callable[[], bool]) -> str:
        """Public text-turn entry point. Mirrors Session.send_text()."""
        # Serialize cancel-and-launch so concurrent send_text() calls
        # cannot both observe the same prev task and launch parallel turns.
        async with self._agent_turn_lock:
            if not admit():
                raise RuntimeError("Session is stopping")
            await self.cancel_application_prompt()
            await self._cancel_active_text_turn(source="text_session")
            # The cancellation calls above suspend. Stop may close admission and
            # snapshot active text work while this caller is waiting, so recheck
            # at the actual task-publication boundary. There is no await between
            # this check and assigning ``_active_text_turn``.
            if not admit():
                raise RuntimeError("Session is stopping")
            token = CancelToken()
            turn_id = f"turn-{uuid4().hex[:12]}"
            self._text_turn_cancel_token = token
            task = self._runtime_scope.create_journaled_task(
                self._execute_text_turn(text, token, turn_id=turn_id),
                name=self._TEXT_TURN_TASK_NAME,
                journal_sink=self._journal_sink,
                turn_id=turn_id,
            )
            task.add_done_callback(self._runtime_scope.log_task_exception)
            self._active_text_turn = task
        try:
            return await task
        finally:
            self._runtime_scope.discard(task)
            if self._active_text_turn is task:
                self._active_text_turn = None
            if self._text_turn_cancel_token is token:
                self._text_turn_cancel_token = None

    async def prompt_agent(  # noqa: C901, PLR0912
        self,
        text: str,
        *,
        role: Literal["system", "user"],
        speak: bool,
        admit: Callable[[], bool],
    ) -> str:
        """Run one application-authored agent turn, optionally through TTS."""
        async with self._agent_turn_lock:
            if not admit():
                raise RuntimeError("Session is stopping")
            await self._cancel_active_text_turn(source="application_prompt")
            await self.cancel_application_prompt()
            if not admit():
                raise RuntimeError("Session is stopping")
            activity = self._turn_manager.capture_activity()
            if activity.guard() and activity.value is not TurnManagerState.IDLE:
                # VAD/PTT can acquire turn ownership after Session.prompt_agent()
                # performs its first cancellation. Re-cancel at the actual
                # admission point, then reserve the application turn without
                # another await so voice work cannot race back in.
                await self._cancel_turn()
                if not admit():
                    raise RuntimeError("Session is stopping")
                # Re-guard: _cancel_turn yields, so VAD may have raced to USER_SPEAKING (gh 1048).
                if not activity.guard():
                    activity = self._turn_manager.capture_activity()
                    if activity.guard() and activity.value is not TurnManagerState.IDLE:
                        await self._cancel_turn()
                        if not admit():
                            raise RuntimeError("Session is stopping")
                        # Final guard - if still not IDLE, voice owns turn now
                        if self._turn_manager.state is not TurnManagerState.IDLE:
                            raise RuntimeError(
                                "Cannot start an application turn while turn manager is "
                                f"{self._turn_manager.state.value}"
                            )
                elif self._turn_manager.state is not TurnManagerState.IDLE:
                    # Activity lease still valid but state changed - voice raced in
                    activity = self._turn_manager.capture_activity()
                    if activity.guard() and activity.value is not TurnManagerState.IDLE:
                        await self._cancel_turn()
                        if self._turn_manager.state is not TurnManagerState.IDLE:
                            raise RuntimeError(
                                "Cannot start an application turn while turn manager is "
                                f"{self._turn_manager.state.value}"
                            )
            # Final check that state is IDLE before admitting
            if self._turn_manager.state is not TurnManagerState.IDLE:
                raise RuntimeError(
                    f"Cannot start an application turn while turn manager is {self._turn_manager.state.value}"
                )
            token = CancelToken()
            turn_id = f"turn-{uuid4().hex[:12]}"
            turn = TurnContext(turn_id=turn_id, cancel_token=token)
            self._turn_manager.begin_application_turn(turn_id, token)
            self._turn.set(turn)
            publication = TurnPublication(
                source="application",
                session_id=self._session_id,
                turn_id=turn_id,
                cancel_token=token,
                activity=self._turn_manager.capture_activity(),
                identity=self._turn.capture_identity(),
            )
            self._application_prompt_cancel_token = token
            self._application_turn_ids.add(turn_id)
            coroutine = self._execute_application_prompt(
                text,
                token,
                turn=turn,
                turn_id=turn_id,
                role=role,
                speak=speak,
                publication=publication,
            )
            task = self._runtime_scope.create_journaled_task(
                coroutine,
                name=self._APPLICATION_PROMPT_TASK_NAME,
                journal_sink=self._journal_sink,
                turn_id=turn_id,
            )
            task.add_done_callback(self._runtime_scope.log_task_exception)
            self._active_application_prompt = task
        try:
            return await task
        finally:
            self._runtime_scope.discard(task)
            self._application_turn_ids.discard(turn_id)
            if self._active_application_prompt is task:
                self._active_application_prompt = None
            if self._application_prompt_cancel_token is token:
                self._application_prompt_cancel_token = None

    def _application_agent_input(
        self,
        text: str,
        role: Literal["system", "user"],
    ) -> tuple[str, str | None]:
        caller_prefix = self._caller_id_system_message()
        if role != "system":
            return text, caller_prefix
        application_prefix = (
            f"The application supplied this system instruction for the current turn:\n{text}"
        )
        system_prefix = (
            f"{caller_prefix}\n\n{application_prefix}" if caller_prefix else application_prefix
        )
        return _APPLICATION_SYSTEM_TRIGGER, system_prefix

    async def _execute_application_prompt(
        self,
        text: str,
        token: CancelToken,
        *,
        turn: TurnContext,
        turn_id: str,
        role: Literal["system", "user"],
        speak: bool,
        publication: TurnPublication,
    ) -> str:
        agent_text, system_prefix = self._application_agent_input(text, role)
        try:
            if speak:
                return await self._execute_spoken_application_prompt(
                    agent_text,
                    token,
                    turn=turn,
                    role=role,
                    system_prefix=system_prefix,
                    publication=publication,
                )
            return await self._execute_text_turn(
                agent_text,
                token,
                turn_id=turn_id,
                system_prefix_override=system_prefix,
                input_role=role,
                publication=publication,
            )
        finally:
            if self._publication_owns_turn(publication, turn):
                self._reset_turn_state()

    async def _execute_spoken_application_prompt(
        self,
        text: str,
        token: CancelToken,
        *,
        turn: TurnContext,
        role: Literal["system", "user"],
        system_prefix: str | None,
        publication: TurnPublication,
    ) -> str:
        turn_token = bind_turn(turn.id)
        try:
            if not self._publication_owns_turn(publication, turn):
                return ""
            # Admitting a spoken application turn re-enables playback, exactly
            # like voice admission above: suppression from a predecessor's
            # replacement conflict (or an explicit playback cancel) must not
            # silence this turn's TTS.
            self._tts.set_playback_suppressed(False)
            await self._emit_turn_started_observation(publication)
            if not self._publication_owns_turn(publication, turn):
                return ""
            await self._emit(TurnEnded(session_id=self._session_id, turn_id=turn.id))
            if not self._publication_owns_turn(publication, turn):
                return ""
            await self._emit(AgentRequestStarted(session_id=self._session_id, turn_id=turn.id))
            if not self._publication_owns_turn(publication, turn):
                return ""
            return await self.run_streaming_agent(
                text,
                token,
                turn=turn,
                identity=publication.identity,
                activity=publication.activity,
                system_prefix_override=system_prefix,
                input_role=role,
            )
        finally:
            reset_turn(turn_token)

    async def _emit_turn_started_observation(self, publication: TurnPublication) -> None:
        """Expose a completed private publication without leaking its leases."""
        await self._emit(
            _mark_turn_started_observation(
                TurnStarted(
                    session_id=publication.session_id,
                    turn_id=publication.turn_id,
                )
            )
        )

    async def _stream_text_turn(
        self,
        text: str,
        cancel_token: CancelToken | None,
        *,
        turn_id: str,
        system_prefix_override: str | None = None,
        input_role: Literal["system", "user"] = "user",
        is_current: Callable[[], bool] = lambda: True,
    ) -> tuple[str, object | None]:
        """Drive the agent stream for a text turn; returns (text, structured output).

        Progress is mirrored onto ``_text_turn_accumulated`` so a barge-in
        ``send_text`` can report what had already been delivered.
        """
        state = _TextTurnStreamState()
        # Build a turn context for this text turn so AgentStage can
        # stamp records with the right turn_id.
        text_turn = TurnContext(turn_id=turn_id, cancel_token=cancel_token or CancelToken())
        system_prefix = (
            system_prefix_override
            if system_prefix_override is not None
            else self._caller_id_system_message()
        )
        stream = self._agent_stage.execute_streaming(
            text,
            self._run_ctx,
            text_turn,
            cancel_token=cancel_token,
            system_prefix=system_prefix,
            input_role=input_role,
            commit_guard=is_current,
        )
        try:
            async for event in stream:
                # A bridge can ignore the cooperative cancel token and yield
                # after the application prompt has been cancelled. Suppress
                # stale assistant output while still draining lifecycle events
                # for tools that were already in flight.
                if not is_current() or (cancel_token and cancel_token.is_cancelled):
                    if not await self._drain_cancelled_text_event(event, state, turn_id):
                        break
                    continue
                if await self._consume_text_event(event, state, turn_id):
                    break
        finally:
            await stream.aclose()
        return state.accumulated, state.structured_output

    async def _drain_cancelled_text_event(
        self,
        event: AgentBridgeEvent,
        state: _TextTurnStreamState,
        turn_id: str,
    ) -> bool:
        """Drain lifecycle events only for tools observed before cancellation."""
        kind = getattr(event, "kind", None)
        call_id = getattr(event, "call_id", None)
        if not state.pending_tool_calls or kind == "done":
            return False
        if kind == "tool_result" and state.pending_tool_calls[call_id] > 0:
            self._finish_text_tool_call(state, call_id)
            await self._emit_text_tool_event(event, kind, turn_id)
        elif kind == "tool_delta" and state.pending_tool_calls[call_id] > 0:
            await self._emit_text_tool_event(event, kind, turn_id)
        return bool(state.pending_tool_calls)

    async def _consume_text_event(
        self,
        event: AgentBridgeEvent,
        state: _TextTurnStreamState,
        turn_id: str,
    ) -> bool:
        """Consume one live text-turn event; return whether it is terminal."""
        kind = getattr(event, "kind", None)
        if kind is None:
            return False
        if kind == "done":
            if getattr(event, "text", ""):
                state.accumulated = event.text
                state.text_stream.replace_final(event.text)
            if getattr(event, "structured_output", None) is not None:
                state.structured_output = event.structured_output
            return True
        if kind in {"text_delta", "text_replace"}:
            update = state.text_stream.apply(event)
            if update is None:  # pragma: no cover - guarded by kind
                return False
            state.accumulated = update.text
            self._text_turn_accumulated = state.accumulated
            if update.text == update.previous_text:
                return False
            await self._emit(
                AgentDelta(
                    text=event.text,
                    part_index=update.part_index,
                    replacement=update.operation == "replace",
                    session_id=self._session_id,
                    turn_id=turn_id,
                )
            )
            return False
        if kind == "tool_started":
            state.pending_tool_calls[getattr(event, "call_id", None)] += 1
        elif kind == "tool_result":
            self._finish_text_tool_call(state, getattr(event, "call_id", None))
        await self._emit_text_tool_event(event, kind, turn_id)
        return False

    @staticmethod
    def _finish_text_tool_call(state: _TextTurnStreamState, call_id: str | None) -> None:
        remaining = state.pending_tool_calls[call_id] - 1
        if remaining > 0:
            state.pending_tool_calls[call_id] = remaining
        else:
            state.pending_tool_calls.pop(call_id, None)

    async def _emit_text_tool_event(
        self,
        event: AgentBridgeEvent,
        kind: str,
        turn_id: str,
    ) -> None:
        # tool_started / tool_delta / tool_result share the same event
        # translation as the voice path so the two surfaces cannot drift.
        await emit_tool_event(
            event,
            kind,
            emit=self._emit,
            session_id=self._session_id,
            turn_id=turn_id,
            tool_span=lambda: observability.span(
                "easycat.agent.tool",
                {
                    "easycat.stage": "agent",
                    "easycat.surface": "agent_bridge",
                },
            ),
        )

    async def _execute_text_turn(
        self,
        text: str,
        cancel_token: CancelToken | None = None,
        *,
        turn_id: str,
        system_prefix_override: str | None = None,
        input_role: Literal["system", "user"] = "user",
        publication: TurnPublication | None = None,
    ) -> str:
        response = ""
        t0 = time.monotonic()
        result_attr = "fail"
        turn_token = bind_turn(turn_id)
        publication = publication or TurnPublication(
            source="text",
            session_id=self._session_id,
            turn_id=turn_id,
            cancel_token=cancel_token,
            activity=None,
        )
        try:
            if not self._text_publication_is_current(publication):
                return ""
            await self._emit_turn_started_observation(publication)
            if not self._text_publication_is_current(publication):
                return ""
            await self._emit(AgentRequestStarted(session_id=self._session_id, turn_id=turn_id))
            if not self._text_publication_is_current(publication):
                return ""
            self._text_turn_accumulated = ""
            response, structured_output = await self._stream_text_turn(
                text,
                cancel_token,
                turn_id=turn_id,
                system_prefix_override=system_prefix_override,
                input_role=input_role,
                is_current=lambda: self._text_publication_is_current(publication),
            )
            elapsed_ms = (time.monotonic() - t0) * 1000
            if not (
                cancel_token and cancel_token.is_cancelled
            ) and self._text_publication_is_current(publication):
                await self._emit(
                    AgentFinal(
                        text=response,
                        structured_output=structured_output,
                        session_id=self._session_id,
                        turn_id=turn_id,
                    )
                )
            if self._journal_enabled:
                self._journal_sink.append_record(
                    kind=JournalRecordKind.METRIC,
                    name="text_turn_latency_ms",
                    turn_id=turn_id,
                    data={"value": elapsed_ms, "surface": "text_session"},
                )
            result_attr = "pass"
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.exception("Agent error in text_session send_text")
            observability.increment_counter(
                "easycat.session.errors.total",
                attributes={
                    "easycat.error_type": type(exc).__name__,
                    "easycat.surface": "agent_bridge",
                },
            )
            await self._emit(
                Error(
                    exception=exc,
                    stage=ErrorStage.AGENT,
                    session_id=self._session_id,
                    turn_id=turn_id,
                    elapsed_ms=elapsed_ms,
                )
            )
            raise
        finally:
            try:
                with observability.span(
                    "easycat.turn.commit",
                    {
                        "easycat.surface": "agent_bridge",
                        "easycat.result": result_attr,
                    },
                ):
                    observability.record_histogram(
                        "easycat.turn.latency",
                        time.monotonic() - t0,
                        {"easycat.surface": "agent_bridge", "easycat.result": result_attr},
                    )
                    observability.increment_counter(
                        "easycat.turns.total",
                        attributes={
                            "easycat.surface": "agent_bridge",
                            "easycat.result": result_attr,
                        },
                    )
                    if self._text_publication_is_current(publication):
                        await self._emit(TurnEnded(session_id=self._session_id, turn_id=turn_id))
            finally:
                reset_turn(turn_token)
        return response

    def _text_publication_is_current(self, publication: TurnPublication) -> bool:
        """Guard application text turns; standalone text has no voice identity."""
        if publication.identity is None:
            return True
        turn = publication.identity.value
        return turn is not None and self._publication_owns_turn(publication, turn)
