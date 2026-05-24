"""Owns TTS payload preparation and synthesis for a Session.

Responsibilities:

- Apply output processors (markdown stripping, phonetic replacement,
  pauses) to raw agent text and produce a :class:`TTSInput` payload.
- Drive the underlying :class:`TTSSynthesizer` to produce audio chunks
  and feed them to the outbound audio queue owned by :class:`AudioRouter`.
- Provide the single-shot :meth:`synthesize_bypass` path used by
  greeting / opt-out announcements.
- Track the in-flight synthesis task so cancellation can target it.
- Reserve the future :meth:`synthesize_sentences` hook for
  sentence-level pipelining (see workstream-tts-pipelining when it lands).

Outbound queue ownership note: the :class:`BoundedAudioQueue` that
carries TTS audio out to the transport lives on :class:`Session`
because :class:`Session.start` may rebuild it when the previous queue
was closed.  :class:`AudioRouter` holds the live reference for draining
and :class:`TTSSynthesizer` (owned by this scheduler) holds the same
reference for writing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from easycat._bounded_queue import BoundedAudioQueue
from easycat._tts_synthesizer import TTSSynthesizer
from easycat.cancel import CancelToken
from easycat.events import EventBus
from easycat.llm_output_processing import (
    LLMOutputProcessor,
    apply_output_processors,
)
from easycat.providers import TTSProvider
from easycat.runtime.context import RunContext
from easycat.session._journal_sink import SessionJournalSink
from easycat.stages.tts import TTSStage
from easycat.timeouts import TimeoutConfig, TTSTimeoutError
from easycat.tts.input import TTSInput, strip_ssml_tags
from easycat.turn_manager import TurnManager, TurnManagerState

if TYPE_CHECKING:
    from easycat.session._audio_router import AudioRouter
    from easycat.session._turn_context import TurnContext

logger = logging.getLogger(__name__)


class TTSScheduler:
    """Prepares and synthesizes TTS payloads for a Session."""

    def __init__(
        self,
        *,
        tts: Callable[[], TTSProvider],
        tts_stage: TTSStage,
        turn_manager: TurnManager,
        event_bus: EventBus,
        journal_sink: SessionJournalSink,
        run_ctx: RunContext,
        no_turn: TurnContext,
        audio_router: AudioRouter,
        outbound_queue: BoundedAudioQueue,
        timeout_config: TimeoutConfig | None,
        correlation_ids: Callable[[], tuple[str | None, str | None]],
        audio_gate: Callable[[], bool] | None,
        # Config
        output_processors: list[LLMOutputProcessor],
        strip_markdown_enabled: bool,
        # Callbacks
        current_turn: Callable[[], TurnContext | None],
        is_gated: Callable[[], bool],
        drain_session_actions: Callable[[], Awaitable[bool]],
        clear_turn: Callable[[], None],
    ) -> None:
        self._tts_getter = tts
        self._tts_stage = tts_stage
        self._turn_manager = turn_manager
        self._event_bus = event_bus
        self._journal_sink = journal_sink
        self._run_ctx = run_ctx
        self._no_turn = no_turn
        self._audio_router = audio_router

        self._output_processors = output_processors
        self._strip_markdown = strip_markdown_enabled

        self._current_turn = current_turn
        self._is_gated = is_gated
        self._drain_session_actions = drain_session_actions
        self._clear_turn = clear_turn

        self._synth = TTSSynthesizer(
            tts=tts(),
            event_bus=event_bus,
            outbound_queue=outbound_queue,
            timeout_config=timeout_config,
            correlation_ids=correlation_ids,
            audio_gate=audio_gate,
        )
        self._synth.bind_stage(
            tts_stage,
            run_ctx_getter=lambda: self._run_ctx,
            turn_getter=lambda: self._current_turn() or self._no_turn,
        )

        self._current_tts_task: asyncio.Task[None] | None = None
        self._playback_suppressed: bool = False

    # ── Properties ─────────────────────────────────────────────

    @property
    def is_playback_suppressed(self) -> bool:
        return self._playback_suppressed

    def set_playback_suppressed(self, value: bool) -> None:
        self._playback_suppressed = value

    @property
    def current_task(self) -> asyncio.Task[None] | None:
        return self._current_tts_task

    @current_task.setter
    def current_task(self, task: asyncio.Task[None] | None) -> None:
        self._current_tts_task = task

    @property
    def strip_markdown_enabled(self) -> bool:
        return self._strip_markdown

    @property
    def synthesizer(self) -> TTSSynthesizer:
        """Underlying :class:`TTSSynthesizer` for low-level access.

        Session ``start()`` uses this to swap the outbound queue when
        rebuilding the queue after a prior teardown.
        """
        return self._synth

    def replace_outbound_queue(self, queue: BoundedAudioQueue) -> None:
        """Point the underlying synthesizer at a rebuilt outbound queue.

        Session.start() re-creates the queue after a prior teardown; this
        keeps the producer side (the synthesizer) pointed at the same
        instance the :class:`AudioRouter` drains, without Session reaching
        into synthesizer internals.
        """
        self._synth.replace_outbound_queue(queue)

    # ── Payload preparation ────────────────────────────────────

    def prepare(self, text: str, *, is_streaming: bool, is_final: bool) -> TTSInput:
        original_payload = TTSInput(text=text, format="plain")
        payload = original_payload
        payload = apply_output_processors(
            payload,
            self._output_processors,
            is_final=is_final,
            is_streaming=is_streaming,
        )
        if payload.format == "ssml" and not getattr(self._tts_getter(), "supports_ssml", False):
            payload = TTSInput(text=strip_ssml_tags(payload.text), format="plain")
        self._record_tts_payload_prepared(
            original_text=original_payload.text,
            original_format=original_payload.format,
            prepared_payload=payload,
            is_streaming=is_streaming,
            is_final=is_final,
        )
        return payload

    # ── Synthesis ──────────────────────────────────────────────

    async def synthesize(
        self,
        payload: TTSInput | str,
        token: CancelToken | None,
        *,
        turn: TurnContext | None = None,
        is_active: Callable[[], bool] | None = None,
    ) -> bool:
        """Synthesize TTS for a complete payload and emit audio events.

        Returns ``True`` if a drained session action signalled that the
        session should stop.

        ``is_active`` is accepted for callers that want to drive the
        synthesizer's per-chunk gating themselves (the streaming agent
        loop does); when ``None`` we fall back to today's behaviour of
        gating on ``TurnManager.state == BOT_SPEAKING`` outside the
        classification gate.
        """
        should_stop = False
        if isinstance(payload, str):
            payload = self.prepare(payload, is_streaming=False, is_final=True)
        if turn is None:
            turn = self._current_turn()
        gated = self._is_gated()
        if not gated:
            await self._turn_manager.bot_started_speaking()
        try:
            effective_is_active = is_active or (
                None
                if gated
                else lambda: self._turn_manager.state == TurnManagerState.BOT_SPEAKING
            )
            result = await self._synth.synthesize(
                payload,
                token,
                is_active=effective_is_active,
            )
            if result.first_audio_time is not None and turn:
                turn.first_tts_audio_time = result.first_audio_time
        except (asyncio.CancelledError, TTSTimeoutError):
            pass
        finally:
            if (
                not gated
                and self._current_turn() is turn
                and turn is not None
                and self._turn_manager.state == TurnManagerState.BOT_SPEAKING
            ):
                # Drain session actions (end_call, transfer) BEFORE
                # transitioning to IDLE so no new turn can sneak in.
                should_stop = await self._drain_session_actions()
                if should_stop:
                    await self._audio_router.await_drain()
                    await self._turn_manager.bot_stopped_speaking()
                else:
                    await self._turn_manager.bot_stopped_speaking()
                    await self._audio_router.await_drain()
                # Only clear if a new turn hasn't started during the drain.
                if self._current_turn() is turn:
                    self._clear_turn()
            elif gated and self._current_turn() is turn and turn is not None:
                # Gated opener TTS is buffered — reset to IDLE so the
                # callee's speech can start new turns while we wait for
                # classification.  Keep the active turn alive so that
                # when the gate flushes and replays buffered audio,
                # the router can still call record_audio_sent() and
                # send playback marks.
                self._audio_router.reset_speech_detection()
                self._turn_manager.reset()
        return should_stop

    async def synthesize_bypass(self, text: str) -> None:
        """Synthesize text via TTS, bypassing the classification gate.

        Used for hold audio and screening responses that must reach the
        transport even while the gate is closed.
        """
        await self._synth.synthesize(text, token=None, bypass_gate=True)

    async def synthesize_sentences(
        self,
        payloads: object,
        cancel_token: CancelToken | None,
        turn: TurnContext,
    ) -> object:
        """Synthesize a stream of payloads with lookahead pipelining.

        Not yet implemented — current behaviour is one-at-a-time
        synthesis via :meth:`synthesize`.  The hook exists so the
        pipelining change is a local one when it lands.
        """
        raise NotImplementedError("sentence-level TTS pipelining is not implemented yet")

    # ── Cancellation ───────────────────────────────────────────

    async def cancel(self) -> None:
        await self._synth.cancel()
        current_task = asyncio.current_task()
        if (
            self._current_tts_task
            and self._current_tts_task is not current_task
            and not self._current_tts_task.done()
        ):
            self._current_tts_task.cancel()
            try:
                await self._current_tts_task
            except (asyncio.CancelledError, Exception):
                pass

    # ── Journal helpers ────────────────────────────────────────

    def _record_markdown_strip(
        self,
        *,
        phase: str,
        original_text: str,
        stripped_text: str,
        turn_id: str | None = None,
    ) -> None:
        """Append a journal record when final-response markdown stripping runs."""
        self._journal_sink.append_record(
            name="markdown_stripped",
            turn_id=turn_id,
            data={
                "phase": phase,
                "changed": original_text != stripped_text,
                "original_text": original_text,
                "stripped_text": stripped_text,
            },
        )

    def _record_tts_payload_prepared(
        self,
        *,
        original_text: str,
        original_format: str,
        prepared_payload: TTSInput,
        is_streaming: bool,
        is_final: bool,
        turn_id: str | None = None,
    ) -> None:
        self._journal_sink.append_record(
            name="tts_payload_prepared",
            turn_id=turn_id,
            data={
                "is_streaming": is_streaming,
                "is_final": is_final,
                "changed": (
                    original_text != prepared_payload.text
                    or original_format != prepared_payload.format
                ),
                "original_text": original_text,
                "original_format": original_format,
                "prepared_text": prepared_payload.text,
                "prepared_format": prepared_payload.format,
                "processors": [type(processor).__name__ for processor in self._output_processors],
                "ssml_downgraded": (
                    original_format == "ssml" and prepared_payload.format == "plain"
                ),
            },
        )
