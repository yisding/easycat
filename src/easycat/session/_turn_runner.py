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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from easycat import _observability as observability
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
)
from easycat.integrations.agents._agent_runner import PreparedAgentResponse
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
from easycat.timeouts import (
    AgentTimeoutError,
    TimeoutConfig,
    TTSTimeoutError,
    with_agent_timeout,
)
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManager, TurnManagerState

if TYPE_CHECKING:
    from easycat.session._audio_router import AudioRouter
    from easycat.session._cancel_orchestrator import CancelOrchestrator
    from easycat.session._stt_committer import STTCommitter
    from easycat.session._tts_scheduler import TTSScheduler
    from easycat.session._wiring import SessionWiringContext
    from easycat.stages.stt import STTStage

logger = logging.getLogger(__name__)


@dataclass
class _StreamingTtsState:
    """Mutable per-turn TTS state shared between the streaming phases.

    Replaces the closure locals that ``run_streaming_agent`` used to share
    with its nested ``_process_tts`` consumer.
    """

    turn: TurnContext
    turn_gen: int
    token: CancelToken | None
    queue: asyncio.Queue[TTSInput | None]
    #: Released after first-payload lifecycle dispatch (or a no-audio terminal
    #: path) so AgentFinal cannot overtake BotStartedSpeaking.
    first_tts_lifecycle_ready: asyncio.Event = field(default_factory=asyncio.Event)
    #: Released after the outer task has emitted (or intentionally skipped)
    #: AgentFinal so fast TTS completion cannot overtake agent output ordering.
    agent_output_settled: asyncio.Event = field(default_factory=asyncio.Event)
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


class TurnRunner:
    """Drives the per-turn agent loop."""

    _TEXT_TURN_TASK_NAME = "text_turn"
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
        self._turn = turn_handle
        self._stt_stage = stt_stage
        self._stt_provider = wiring.stt
        self._is_running = wiring.is_running
        self._is_gated = wiring.is_gated
        self._agent = wiring.agent
        self._drain_session_actions = wiring.drain_session_actions
        self._caller_id_system_message = wiring.caller_id_system_message
        self._stop = wiring.stop
        self._reset_turn_state = wiring.reset_turn_state
        self._emit = wiring.emit
        self._session_id = session_id
        self._journal_enabled = journal_enabled

        # Active text-turn tracking.
        self._active_text_turn: asyncio.Task[str] | None = None
        self._text_turn_cancel_token: CancelToken | None = None
        self._text_turn_accumulated: str = ""
        self._text_turn_lock = asyncio.Lock()

        # Voice-only speculative generation. The task may run while the turn
        # manager is confirming an endpoint, but its result is not committed
        # to agent history until ``handle_end_of_speech`` confirms that the
        # transcript still matches.
        self._preemptive_task: asyncio.Task[_PreemptiveAgentResult] | None = None
        self._preemptive_transcript = ""
        self._preemptive_turn_generation = 0
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

    # ── Subscription handlers ─────────────────────────────────────

    async def on_turn_started(self, event: TurnStarted) -> None:
        """Handle TurnStarted from TurnManager: start STT and prime pre-roll."""
        if not self._is_running():
            return
        await self.cancel_preemptive_generation()
        # TurnManager always stamps TurnStarted with a generated id;
        # synthesize one for hand-built events so the TurnContext (and
        # every journal record keyed off it) still gets a real id.
        turn_id = event.turn_id or f"turn-{uuid4().hex[:8]}"

        # Cancel the previous turn's token so any in-flight agent/TTS work
        # notices the cancellation before we overwrite the turn pointer.
        prev = self._turn.current
        self._stt.cancel_scheduled()
        self._stt.cancel_inflight()
        self._stt.resolve_pending(prev, "")

        if prev and not prev.cancel_token.is_cancelled:
            prev.cancel_token.cancel()

        cancel_token = self._turn_manager.cancel_token or CancelToken()
        turn = TurnContext(turn_id=turn_id, cancel_token=cancel_token)
        self._turn.set(turn)
        self._preemptive_turn_generation = turn.generation
        self._preemptive_attempts = 0
        # Tag startup records for this turn without leaving the EventBus task
        # pinned to the turn after this handler returns.
        turn_token = bind_turn(turn.id)
        try:
            self._audio.reset_speech_detection()
            self._tts.set_playback_suppressed(False)

            # Start STT stream
            stt = self._stt_provider()
            await stt.start_stream()
            self._stt.mark_active()

            # Prime STT with pre-roll frames captured by TurnManager.
            # The background event consumer is started only after the stream
            # is open and pre-roll priming succeeds, so a failure here cannot
            # leave an orphaned consumer task running against a half-open
            # stream for the rest of the session.
            for chunk in self._turn_manager.turn_audio:
                await self._stt_stage.execute(chunk, self._run_ctx, turn)
                turn.stt_has_uncommitted_audio = True

            self._stt.start_event_loop(turn)
        except Exception as exc:
            logger.exception("Failed to start STT stream")
            await self._emit(Error(exception=exc, stage=ErrorStage.STT))
            # Full per-turn teardown: close the (possibly half-open) stream,
            # cancel/await any STT consumer task, mark inactive, and resolve
            # pending futures so no live STT work or stale turn is left behind.
            try:
                await self._stt.cancel(turn)
            except Exception:
                logger.debug("STT teardown after start failure raised", exc_info=True)
            # Clear the turn pointer and return the TurnManager toward IDLE so
            # the caller doesn't sit in USER_SPEAKING until the silence timeout.
            if self._turn.current is turn:
                self._reset_turn_state()
            return
        finally:
            reset_turn(turn_token)

    async def on_stt_final(self, event: STTFinal) -> None:
        """Start history-isolated agent work while endpointing is still pending."""
        candidate = self._preemptive_candidate(event)
        if candidate is None:
            return
        turn, transcript = candidate
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
        if self._preemptive_turn_generation != turn.generation:
            self._preemptive_turn_generation = turn.generation
            self._preemptive_attempts = 0
        if self._preemptive_attempts >= self._agent_stage.preemptive_max_retries:
            return

        self._preemptive_attempts += 1
        self._preemptive_transcript = transcript
        self._preemptive_turn_generation = turn.generation

        async def _prepare() -> _PreemptiveAgentResult:
            try:
                response = await self._agent_stage.prepare_preemptive(transcript, turn)
                return _PreemptiveAgentResult(response=response)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return _PreemptiveAgentResult(error=exc)

        self._preemptive_task = self._runtime_scope.create_journaled_task(
            _prepare(),
            name=self._PREEMPTIVE_TASK_NAME,
            journal_sink=self._journal_sink,
            turn_id=turn.id,
        )

    def _preemptive_candidate(self, event: STTFinal) -> tuple[TurnContext, str] | None:
        """Return the active turn/transcript when speculative work is safe."""
        if not self._agent_stage.supports_preemptive_generation:
            return None
        turn = self._turn.current
        if turn is None or turn.cancel_token.is_cancelled:
            return None
        if self._preemptive_take_passed(turn):
            return None
        if event.turn_id is not None and event.turn_id != turn.id:
            return None

        transcript = turn.transcript_text
        if not transcript:
            return None
        return turn, transcript

    def _preemptive_matches(self, turn: TurnContext, transcript: str) -> bool:
        """Whether the active attempt already targets this exact transcript."""
        return bool(
            self._preemptive_task is not None
            and self._preemptive_turn_generation == turn.generation
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
        self._stt.cancel_scheduled()
        self._stt.cancel_inflight()
        current_tts_task = self._tts.current_task
        if current_tts_task and not current_tts_task.done():
            current_tts_task.cancel()
        gen = self._turn.generation
        turn = self._turn.current
        turn_token = bind_turn(event.turn_id)
        try:
            new_task = self._runtime_scope.create_journaled_task(
                self.on_turn_ended(event, gen, turn=turn),
                name="on_turn_ended",
                journal_sink=self._journal_sink,
                turn_id=event.turn_id,
            )
        finally:
            reset_turn(turn_token)
        self._tts.current_task = new_task
        new_task.add_done_callback(self._runtime_scope.log_task_exception)

    async def on_turn_ended(
        self,
        event: TurnEnded,
        generation: int,
        turn: TurnContext | None = None,
    ) -> None:
        """Handle TurnEnded from TurnManager: finalize STT and run agent/TTS."""
        turn_token = bind_turn(event.turn_id)
        try:
            if self._turn.generation != generation:
                return
            if turn and turn.cancel_token.is_cancelled:
                return
            if self._turn_manager.state != TurnManagerState.PROCESSING:
                return
            if turn:
                turn.end_time = event.timestamp
            await self.handle_end_of_speech(turn=turn)
        finally:
            reset_turn(turn_token)

    # ── Pipeline ───────────────────────────────────────────────────

    async def handle_end_of_speech(self, turn: TurnContext | None = None) -> None:
        """Finalize STT, run the agent, synthesize TTS.

        ``turn`` defaults to the active session turn for backwards
        compatibility; internal callers always pass it explicitly.
        """
        if turn is None:
            turn = self._turn.current
        token = turn.cancel_token if turn else None
        turn_generation = self._turn.generation
        if turn is not None:
            # This turn is now past its take point: a trailing STTFinal (a
            # provider can flush a second final segment during the
            # ``end_stream`` drain below) must not start new speculation
            # that would overlap the confirmed ``run()`` for this turn.
            self._preemptive_finalized_generation = max(
                self._preemptive_finalized_generation, turn.generation
            )

        transcript = await self._finalize_turn_transcript(turn)

        if not transcript or (token and token.is_cancelled):
            await self.cancel_preemptive_generation()
            if self._turn.current is turn:
                self._reset_turn_state()
            return

        await self._emit(AgentRequestStarted())
        prepared_response = await self._take_preemptive_response(transcript, turn)
        # The await above spans the remaining model latency. Speech may resume
        # during it, cancelling/replacing this turn. Never fall through to the
        # confirmed invocation for an abandoned transcript: even a cancelled
        # AgentRunner records its user message before it observes the token.
        if not self._is_active_voice_turn(turn, token, turn_generation):
            return
        await self.run_streaming_agent(
            transcript,
            token,
            turn=turn,
            prepared_response=prepared_response,
        )

    def _is_active_voice_turn(
        self,
        turn: TurnContext | None,
        token: CancelToken | None,
        generation: int,
    ) -> bool:
        """Whether a post-await voice turn is still the session's active generation."""
        return bool(
            turn is not None
            and not (token and token.is_cancelled)
            and self._turn.current is turn
            and self._turn.generation == generation
        )

    async def cancel_preemptive_generation(self) -> None:
        """Cancel and drain the current preemptive task, if any."""
        task = self._preemptive_task
        self._preemptive_task = None
        self._preemptive_transcript = ""
        if task is None:
            return
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
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
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
            or self._preemptive_turn_generation != turn.generation
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

    async def _finalize_turn_transcript(self, turn: TurnContext | None) -> str:
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

        if not await self._stt.await_pending(turn):
            return ""

        if stt_needs_close:
            await self._stt.end_stream(turn)

        if not await self._stt.await_pending(turn):
            return ""

        transcript = ""
        if turn is not None:
            transcript = turn.transcript_text

        if transcript and turn:
            turn.stt_final_time = time.monotonic()
        return transcript

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
        prepared_response: PreparedAgentResponse | None = None,
    ) -> None:
        """Streaming agent path with incremental TTS on sentence boundaries.

        Uses :func:`consume_agent_stream` to translate agent events into
        TTS payloads, and runs TTS synthesis concurrently.  The phases are
        named methods: ``_consume_tts_payloads`` (the TTS consumer task),
        ``_await_agent_task`` (agent timeout/cancellation handling),
        ``_finalize_streamed_text`` (final markdown strip), and
        ``_record_streaming_interruption`` (barge-in accounting).
        """
        if turn is None:
            turn = self._turn.current
        assert turn is not None
        st = _StreamingTtsState(
            turn=turn,
            turn_gen=self._turn.generation,
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
        system_prefix = self._caller_id_system_message()

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
                ),
                cancel_token=token,
                tts_queue=st.queue,
                emit=self._emit,
                prepare_tts_payload=self._tts.prepare,
                strip_md=self._tts.strip_markdown_enabled,
                turn=turn,
            )

        agent_task = asyncio.create_task(_run_agent_consumer())
        tts_task = asyncio.create_task(self._consume_tts_payloads(st))

        try:
            caught_exc = await self._await_agent_task_recording_cancel(st, agent_task, tts_task)
            # Lifecycle ordering is not agent execution time. Wait outside
            # ``_await_agent_task`` so slow BotStartedSpeaking handlers cannot trip
            # the agent timeout after the agent has already completed.
            await self._await_first_tts_lifecycle_ready(st, tts_task)
            agent_error = agent_result.error if agent_result else caught_exc
            interrupted = agent_result.interrupted if agent_result else False
            accumulated_text = agent_result.text if agent_result else ""
            structured_output = agent_result.structured_output if agent_result else None
            stream_succeeded = agent_error is None and not (token and token.is_cancelled)

            if self._tts.strip_markdown_enabled and accumulated_text and stream_succeeded:
                accumulated_text = self._finalize_streamed_text(turn, accumulated_text)

            if (accumulated_text or structured_output is not None) and stream_succeeded:
                await self._emit(
                    AgentFinal(text=accumulated_text, structured_output=structured_output)
                )
        finally:
            st.agent_output_settled.set()

        await self._await_tts_task_recording_cancel(st, tts_task)

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
            return

        self._record_voice_total_latency(turn)

        # If a newer turn started (e.g. barge-in), avoid clobbering its state.
        if self._turn.current is turn and self._turn.generation == st.turn_gen:
            if self._turn_manager.state != TurnManagerState.IDLE:
                self._reset_turn_state()

    # ── Streaming agent phases ─────────────────────────────────────

    async def _consume_tts_payloads(self, st: _StreamingTtsState) -> None:
        """TTS consumer task: synthesize queued payloads, then settle the turn."""
        try:
            await self._synthesize_queued_payloads(st)
        except asyncio.CancelledError:
            pass
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

        await self._settle_turn_after_tts(st)

    async def _synthesize_queued_payloads(self, st: _StreamingTtsState) -> None:
        """Drain the payload queue through the synthesizer until the sentinel."""
        while True:
            first_synthesis_task = None
            payload = await st.queue.get()
            if payload is None:
                st.first_tts_lifecycle_ready.set()
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
                    st.gated = self._is_gated()
                    if not st.gated:
                        first_synthesis_task = await self._tts.begin_synthesis_with_bot_start(
                            payload,
                            st.token,
                            is_active=lambda: (
                                self._turn_manager.state == TurnManagerState.BOT_SPEAKING
                            ),
                        )
                        st.playback_started = True
                finally:
                    st.synth_started = True
                    st.first_tts_lifecycle_ready.set()

            if first_synthesis_task is not None:
                result = await self._await_owned_first_synthesis(first_synthesis_task)
            else:
                result = await self._tts.synthesizer.synthesize(
                    payload,
                    st.token,
                    is_active=(
                        None
                        if self._is_gated()
                        else lambda: self._turn_manager.state == TurnManagerState.BOT_SPEAKING
                    ),
                )
            st.chunks.append(
                TtsChunk(
                    _text_for_estimation_timeline(payload),
                    result.audio_bytes,
                    result.completed,
                )
            )
            if result.first_audio_time is not None and st.turn.first_tts_audio_time is None:
                st.turn.first_tts_audio_time = result.first_audio_time

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
        if st.synth_started and self._turn_manager.state == TurnManagerState.BOT_SPEAKING:
            st.should_stop = await self._tts.finalize_speaking_turn(
                st.turn, turn_generation=st.turn_gen
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
            return await self._await_agent_task(agent_task, tts_task)
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
        self._cancel_pending(tts_task)
        try:
            await tts_task
        except (asyncio.CancelledError, Exception):
            pass
        self._record_streaming_interruption(
            st,
            interrupted=st.turn.last_barge_in_time is not None,
            source="streaming_turn_cancelled",
        )

    async def _await_agent_task(
        self,
        agent_task: asyncio.Task[None],
        tts_task: asyncio.Task[None],
    ) -> Exception | None:
        """Await the agent under its timeout; cancel both tasks on failure."""
        timeout_task: asyncio.Task[None] | None = None
        try:
            if self._timeout_config and self._timeout_config.agent_timeout:
                timeout_task = asyncio.create_task(
                    with_agent_timeout(
                        agent_task,
                        timeout=self._timeout_config.agent_timeout,
                        event_bus=self._event_bus,
                    )
                )
                await asyncio.shield(timeout_task)
            else:
                await asyncio.shield(agent_task)
        except asyncio.CancelledError:
            tasks = (
                (agent_task, tts_task)
                if timeout_task is None
                else (
                    timeout_task,
                    agent_task,
                    tts_task,
                )
            )
            self._cancel_pending(*tasks)
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            raise
        except Exception as exc:
            # AgentTimeoutError is already logged and emitted by with_agent_timeout.
            if not isinstance(exc, AgentTimeoutError):
                logger.exception("Streaming agent error")
                await self._emit(Error(exception=exc, stage=ErrorStage.AGENT))
            self._cancel_pending(agent_task, tts_task)
            return exc
        return None

    @staticmethod
    def _cancel_pending(*tasks: asyncio.Task[None]) -> None:
        for t in tasks:
            if not t.done():
                t.cancel()

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

    async def send_text(self, text: str) -> str:
        """Public text-turn entry point. Mirrors Session.send_text()."""
        # Serialize cancel-and-launch so concurrent send_text() calls
        # cannot both observe the same prev task and launch parallel turns.
        async with self._text_turn_lock:
            prev = self._active_text_turn
            if prev is not None and not prev.done():
                delivered = self._text_turn_accumulated
                if self._text_turn_cancel_token:
                    self._text_turn_cancel_token.cancel()
                prev.cancel()
                try:
                    await prev
                except (asyncio.CancelledError, Exception):
                    pass
                finally:
                    self._runtime_scope.discard(prev)
                notified = _notify_bridge_interruption(
                    self._agent_stage,
                    delivered,
                    self._cancel.interruption_mode,
                    ctx=self._run_ctx,
                )
                self._cancel.record_interruption(
                    source="text_session",
                    mode=self._cancel.interruption_mode,
                    text_spoken=delivered,
                    notified=notified,
                )

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

    async def _stream_text_turn(
        self,
        text: str,
        cancel_token: CancelToken | None,
        *,
        turn_id: str,
    ) -> tuple[str, object | None]:
        """Drive the agent stream for a text turn; returns (text, structured output).

        Progress is mirrored onto ``_text_turn_accumulated`` so a barge-in
        ``send_text`` can report what had already been delivered.
        """
        structured_output: object | None = None
        accumulated = ""
        # Build a turn context for this text turn so AgentStage can
        # stamp records with the right turn_id.
        text_turn = TurnContext(turn_id=turn_id, cancel_token=cancel_token or CancelToken())
        system_prefix = self._caller_id_system_message()
        stream = self._agent_stage.execute_streaming(
            text,
            self._run_ctx,
            text_turn,
            cancel_token=cancel_token,
            system_prefix=system_prefix,
        )
        try:
            async for event in stream:
                kind = getattr(event, "kind", None)
                if kind is None:
                    continue
                if kind == "done":
                    if event.text:
                        accumulated = event.text
                    if getattr(event, "structured_output", None) is not None:
                        structured_output = event.structured_output
                    break
                if kind == "text_delta" and event.text:
                    accumulated += event.text
                    self._text_turn_accumulated = accumulated
                    await self._emit(
                        AgentDelta(
                            text=event.text,
                            session_id=self._session_id,
                            turn_id=turn_id,
                        )
                    )
                else:
                    # tool_started / tool_delta / tool_result share the same
                    # event-translation as the voice path via emit_tool_event,
                    # so the two cannot drift.  The per-tool observability span
                    # is text-path specific and threaded in via tool_span.
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
        finally:
            await stream.aclose()
        return accumulated, structured_output

    async def _execute_text_turn(
        self,
        text: str,
        cancel_token: CancelToken | None = None,
        *,
        turn_id: str,
    ) -> str:
        response = ""
        t0 = time.monotonic()
        result_attr = "fail"
        turn_token = bind_turn(turn_id)
        try:
            await self._emit(TurnStarted(session_id=self._session_id, turn_id=turn_id))
            await self._emit(AgentRequestStarted(session_id=self._session_id, turn_id=turn_id))
            self._text_turn_accumulated = ""
            response, structured_output = await self._stream_text_turn(
                text, cancel_token, turn_id=turn_id
            )
            elapsed_ms = (time.monotonic() - t0) * 1000
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
                    await self._emit(TurnEnded(session_id=self._session_id, turn_id=turn_id))
            finally:
                reset_turn(turn_token)
        return response
