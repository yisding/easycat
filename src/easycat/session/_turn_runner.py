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
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from easycat.cancel import CancelToken
from easycat.events import (
    AgentDelta,
    AgentFinal,
    AgentRequestStarted,
    Error,
    ErrorStage,
    EventBus,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
    TurnEnded,
    TurnStarted,
)
from easycat.runtime.context import RunContext
from easycat.runtime.records import JournalRecordKind
from easycat.runtime.scope import RuntimeScope
from easycat.session._journal_sink import SessionJournalSink
from easycat.session._streaming import consume_agent_stream
from easycat.session._text import _text_for_estimation_timeline
from easycat.session._turn_context import TurnContext, TurnHandle
from easycat.session.interruption import (
    estimate_and_notify_interruption,
)
from easycat.session.interruption import (
    notify_bridge_interruption as _notify_bridge_interruption,
)
from easycat.stages.agent import AgentStage
from easycat.strip_markdown import strip_markdown
from easycat.timeouts import (
    AgentTimeoutError,
    STTTimeoutError,
    TimeoutConfig,
    TTSTimeoutError,
    with_agent_timeout,
)
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManager, TurnManagerState

if TYPE_CHECKING:
    from easycat.providers import STTProvider
    from easycat.session._audio_router import AudioRouter
    from easycat.session._cancel_orchestrator import CancelOrchestrator
    from easycat.session._stt_committer import STTCommitter
    from easycat.session._tts_scheduler import TTSScheduler
    from easycat.session._types import Agent
    from easycat.stages.stt import STTStage

logger = logging.getLogger(__name__)


class TurnRunner:
    """Drives the per-turn agent loop."""

    def __init__(
        self,
        *,
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
        stt_provider: Callable[[], STTProvider],
        is_running: Callable[[], bool],
        is_gated: Callable[[], bool],
        agent: Callable[[], Agent],
        drain_session_actions: Callable[[], Awaitable[bool]],
        caller_id_system_message: Callable[[], str | None],
        stop: Callable[[], Awaitable[None]],
        reset_turn_state: Callable[[], None],
        emit: Callable[[Any], Awaitable[None]],
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
        self._stt_provider = stt_provider
        self._is_running = is_running
        self._is_gated = is_gated
        self._agent = agent
        self._drain_session_actions = drain_session_actions
        self._caller_id_system_message = caller_id_system_message
        self._stop = stop
        self._reset_turn_state = reset_turn_state
        self._emit = emit
        self._session_id = session_id
        self._journal_enabled = journal_enabled

        # Active text-turn tracking.
        self._active_text_turn: asyncio.Task[str] | None = None
        self._text_turn_cancel_token: CancelToken | None = None
        self._text_turn_accumulated: str = ""
        self._text_turn_lock = asyncio.Lock()

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

        # Cancel the previous turn's token so any in-flight agent/TTS work
        # notices the cancellation before we overwrite the turn pointer.
        prev = self._turn.current
        self._stt.cancel_scheduled()
        self._stt.cancel_inflight()
        self._stt.resolve_pending(prev, "")
        if prev is not None and prev is not self._turn.no_turn:
            prev.stt_final_future = None

        if prev and not prev.cancel_token.is_cancelled:
            prev.cancel_token.cancel()

        cancel_token = self._turn_manager.cancel_token or CancelToken()
        turn = TurnContext(turn_id=event.turn_id, cancel_token=cancel_token)
        self._turn.set(turn)
        self._audio.reset_speech_detection()
        self._tts.set_playback_suppressed(False)

        # Start STT stream
        try:
            stt = self._stt_provider()
            await stt.start_stream()
            self._stt.mark_active()
            self._stt.start_event_loop(turn)
        except Exception as exc:
            logger.exception("Failed to start STT stream")
            await self._emit(Error(exception=exc, stage=ErrorStage.STT))
            self._stt.mark_inactive()
            return

        # Prime STT with pre-roll frames captured by TurnManager
        for chunk in self._turn_manager.turn_audio:
            await self._stt_stage.execute(chunk, self._run_ctx, turn)
            turn.stt_has_uncommitted_audio = True

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
        new_task = self._runtime_scope.create_journaled_task(
            self.on_turn_ended(event, gen, turn=turn),
            name="on_turn_ended",
            journal_sink=self._journal_sink,
            turn_id=event.turn_id,
        )
        self._tts.current_task = new_task
        new_task.add_done_callback(self._runtime_scope.log_task_exception)

    async def on_turn_ended(
        self,
        event: TurnEnded,
        generation: int,
        turn: TurnContext | None = None,
    ) -> None:
        """Handle TurnEnded from TurnManager: finalize STT and run agent/TTS."""
        if self._turn.generation != generation:
            return
        if turn and turn.cancel_token.is_cancelled:
            return
        if self._turn_manager.state != TurnManagerState.PROCESSING:
            return
        if turn:
            turn.end_time = event.timestamp
        await self.handle_end_of_speech(turn=turn)

    # ── Pipeline ───────────────────────────────────────────────────

    async def handle_end_of_speech(self, turn: TurnContext | None = None) -> None:
        """Finalize STT, run the agent, synthesize TTS.

        ``turn`` defaults to the active session turn for backwards
        compatibility; internal callers always pass it explicitly.
        """
        if turn is None:
            turn = self._turn.current
        token = turn.cancel_token if turn else None
        self._stt.cancel_scheduled()

        # Stop forwarding audio to STT immediately so trailing frames
        # from continuous transports don't leak into the transcript.
        stt_needs_close = self._stt.is_active
        self._stt.mark_inactive()

        await self._stt.await_inflight_commit()

        if not await self._stt.await_pending(turn):
            if self._turn.current is turn:
                self._reset_turn_state()
            return

        if stt_needs_close:
            await self._stt.end_stream(turn)

        if not await self._stt.await_pending(turn):
            if self._turn.current is turn:
                self._reset_turn_state()
            return

        transcript = ""
        if turn is not None:
            transcript = turn.transcript_text
        stt_final_future = (
            turn.stt_final_future if turn is not None and turn is not self._turn.no_turn else None
        )
        if not transcript and stt_final_future is not None:
            try:
                if self._timeout_config and self._timeout_config.stt_timeout:
                    transcript = await asyncio.wait_for(
                        stt_final_future,
                        timeout=self._timeout_config.stt_timeout,
                    )
                else:
                    transcript = await stt_final_future
            except TimeoutError:
                err = STTTimeoutError("stt", self._timeout_config.stt_timeout)
                await self._emit(Error(exception=err, stage=ErrorStage.STT))
                if self._turn.current is turn:
                    self._reset_turn_state()
                return
            except Exception:
                transcript = ""
            finally:
                if turn is not None and turn is not self._turn.no_turn:
                    turn.stt_final_future = None

        if transcript:
            if turn is not None and turn is not self._turn.no_turn:
                if turn.stt_final_future is not None and not turn.stt_final_future.done():
                    turn.stt_final_future.set_result(transcript)
            if turn:
                turn.stt_final_time = time.monotonic()

        if not transcript or (token and token.is_cancelled):
            if self._turn.current is turn:
                self._reset_turn_state()
            return

        await self._emit(AgentRequestStarted())
        await self.run_streaming_agent(transcript, token, turn=turn)

    # ── Streaming agent path ───────────────────────────────────────

    async def run_streaming_agent(
        self,
        transcript: str,
        token: CancelToken | None,
        *,
        turn: TurnContext | None = None,
    ) -> None:
        """Streaming agent path with incremental TTS on sentence boundaries.

        Uses :func:`consume_agent_stream` to translate agent events into
        TTS payloads, and runs TTS synthesis concurrently.
        """
        if turn is None:
            turn = self._turn.current
        assert turn is not None
        turn_gen = self._turn.generation
        tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue()
        tts_playback_started = False
        tts_chunks: list[tuple[str, int, bool]] = []
        tts_should_stop = False

        # ── TTS consumer task ──

        async def _process_tts() -> None:
            nonlocal tts_should_stop
            nonlocal tts_playback_started
            started = False
            try:
                while True:
                    payload = await tts_queue.get()
                    if payload is None:
                        break
                    if token and token.is_cancelled:
                        tts_chunks.append((_text_for_estimation_timeline(payload), 0, False))
                        break
                    if self._tts.is_playback_suppressed:
                        tts_chunks.append((_text_for_estimation_timeline(payload), 0, False))
                        break

                    if not started:
                        gated = self._is_gated()
                        if not gated:
                            await self._turn_manager.bot_started_speaking()
                            tts_playback_started = True
                        started = True

                    result = await self._tts.synthesizer.synthesize(
                        payload,
                        token,
                        is_active=(
                            None
                            if self._is_gated()
                            else lambda: self._turn_manager.state == TurnManagerState.BOT_SPEAKING
                        ),
                    )
                    tts_chunks.append(
                        (
                            _text_for_estimation_timeline(payload),
                            result.audio_bytes,
                            result.completed,
                        )
                    )
                    if result.first_audio_time is not None and turn.first_tts_audio_time is None:
                        turn.first_tts_audio_time = result.first_audio_time
            except asyncio.CancelledError:
                pass
            except TTSTimeoutError:
                await self._tts.cancel()
            except Exception:
                logger.exception("TTS streaming error")

            while not tts_queue.empty():
                remaining = tts_queue.get_nowait()
                if remaining is not None:
                    tts_chunks.append((_text_for_estimation_timeline(remaining), 0, False))

            if started and self._turn_manager.state == TurnManagerState.BOT_SPEAKING:
                # Drain session actions (end_call, transfer) BEFORE
                # transitioning to IDLE so no new turn can sneak in.
                tts_should_stop = await self._drain_session_actions()
                if tts_should_stop:
                    await self._audio.await_drain()
                    await self._turn_manager.bot_stopped_speaking()
                else:
                    await self._turn_manager.bot_stopped_speaking()
                    # Wait for queued audio to drain so the router can still
                    # call turn.record_audio_sent() and emit playback marks
                    # for the tail of this turn's audio.
                    await self._audio.await_drain()
                # Only clear if a new turn hasn't started during the drain.
                if self._turn.current is turn and self._turn.generation == turn_gen:
                    self._turn.set(None)
            elif started and not tts_playback_started:
                if self._is_gated():
                    # Keep current turn alive for gated replay mark accounting
                    self._audio.reset_speech_detection()
                    self._turn_manager.reset()
                else:
                    self._reset_turn_state()

        # ── Run agent stream + TTS concurrently ──

        agent_result = None
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
                ),
                cancel_token=token,
                tts_queue=tts_queue,
                emit=self._emit,
                prepare_tts_payload=self._tts.prepare,
                strip_md=self._tts.strip_markdown_enabled,
                turn=turn,
            )

        agent_task = asyncio.create_task(_run_agent_consumer())
        tts_task = asyncio.create_task(_process_tts())

        caught_exc: Exception | None = None
        try:
            if self._timeout_config and self._timeout_config.agent_timeout:
                await with_agent_timeout(
                    agent_task,
                    timeout=self._timeout_config.agent_timeout,
                    event_bus=self._event_bus,
                )
            else:
                await agent_task
        except asyncio.CancelledError:
            if not agent_task.done():
                agent_task.cancel()
            if not tts_task.done():
                tts_task.cancel()
            for t in (agent_task, tts_task):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            raise
        except Exception as exc:
            caught_exc = exc
            # AgentTimeoutError is already logged and emitted by with_agent_timeout.
            if not isinstance(exc, AgentTimeoutError):
                logger.exception("Streaming agent error")
                await self._emit(Error(exception=exc, stage=ErrorStage.AGENT))
            if not agent_task.done():
                agent_task.cancel()
            if not tts_task.done():
                tts_task.cancel()
        agent_error = agent_result.error if agent_result else caught_exc
        interrupted = agent_result.interrupted if agent_result else False
        accumulated_text = agent_result.text if agent_result else ""
        structured_output = agent_result.structured_output if agent_result else None
        stream_succeeded = agent_error is None and not (token and token.is_cancelled)

        if self._tts.strip_markdown_enabled and accumulated_text and stream_succeeded:
            original_text = accumulated_text
            stripped = strip_markdown(accumulated_text, normalize_code_spans=True)
            self._tts._record_markdown_strip(
                phase="streaming_final",
                original_text=original_text,
                stripped_text=stripped,
                turn_id=turn.id,
            )
            if stripped != original_text:
                accumulated_text = stripped
                self._agent().replace_last_assistant_text(stripped)

        if (accumulated_text or structured_output is not None) and stream_succeeded:
            await self._emit(
                AgentFinal(text=accumulated_text, structured_output=structured_output)
            )

        try:
            await tts_task
        except asyncio.CancelledError:
            pass

        interruption_notification = estimate_and_notify_interruption(
            self._agent(),
            token,
            turn,
            tts_chunks,
            tts_playback_started=tts_playback_started,
            interrupted=interrupted,
            interruption_mode=self._cancel.interruption_mode,
            latency_compensation_ms=self._cancel.latency_compensation_ms,
            ack_stale_ms=self._cancel.ack_stale_ms,
            ack_tail_cap_ms=self._cancel.ack_tail_cap_ms,
        )
        if interruption_notification is not None:
            self._cancel.record_interruption(
                source="streaming_turn",
                mode=interruption_notification.mode,
                text_spoken=interruption_notification.text_spoken,
                notified=interruption_notification.notified,
                turn_id=turn.id,
            )

        if tts_should_stop:
            await self._stop()
            return

        # If a newer turn started (e.g. barge-in), avoid clobbering its state.
        if self._turn.current is turn and self._turn.generation == turn_gen:
            if self._turn_manager.state != TurnManagerState.IDLE:
                self._reset_turn_state()

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
                notified = _notify_bridge_interruption(
                    self._agent(), delivered, self._cancel.interruption_mode
                )
                self._cancel.record_interruption(
                    source="text_session",
                    mode=self._cancel.interruption_mode,
                    text_spoken=delivered,
                    notified=notified,
                )

            token = CancelToken()
            self._text_turn_cancel_token = token
            task = asyncio.ensure_future(self._execute_text_turn(text, token))
            self._active_text_turn = task
        return await task

    async def _execute_text_turn(self, text: str, cancel_token: CancelToken | None = None) -> str:
        turn_id = f"turn-{uuid4().hex[:12]}"
        await self._emit(TurnStarted(session_id=self._session_id, turn_id=turn_id))
        response = ""
        try:
            t0 = time.monotonic()
            await self._emit(AgentRequestStarted(session_id=self._session_id, turn_id=turn_id))
            structured_output = None
            self._text_turn_accumulated = ""
            # Build a turn context for this text turn so AgentStage can
            # stamp records with the right turn_id.
            text_turn = TurnContext(turn_id=turn_id, cancel_token=cancel_token or CancelToken())
            accumulated = ""
            system_prefix = self._caller_id_system_message()
            async for event in self._agent_stage.execute_streaming(
                text,
                self._run_ctx,
                text_turn,
                cancel_token=cancel_token,
                system_prefix=system_prefix,
            ):
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
                elif kind == "tool_started":
                    await self._emit(
                        ToolCallStarted(
                            tool_name=event.tool_name,
                            call_id=event.call_id,
                            session_id=self._session_id,
                            turn_id=turn_id,
                        )
                    )
                elif kind == "tool_delta":
                    await self._emit(
                        ToolCallDelta(
                            call_id=event.call_id,
                            delta=event.text,
                            session_id=self._session_id,
                            turn_id=turn_id,
                        )
                    )
                elif kind == "tool_result":
                    await self._emit(
                        ToolCallResult(
                            call_id=event.call_id,
                            result=event.result,
                            session_id=self._session_id,
                            turn_id=turn_id,
                        )
                    )
            response = accumulated
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
                    data={"value": elapsed_ms},
                )
        except Exception as exc:
            logger.exception("Agent error in text_session send_text")
            await self._emit(
                Error(
                    exception=exc,
                    stage=ErrorStage.AGENT,
                    session_id=self._session_id,
                    turn_id=turn_id,
                )
            )
            raise
        finally:
            await self._emit(TurnEnded(session_id=self._session_id, turn_id=turn_id))
        return response
