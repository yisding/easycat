"""Owns TTS payload preparation and synthesis for a Session.

Responsibilities:

- Apply output processors (markdown stripping, phonetic replacement,
  pauses) to raw agent text and produce a :class:`TTSInput` payload.
- Drive the underlying :class:`TTSSynthesizer` to produce audio chunks
  and feed them to the outbound audio queue owned by :class:`AudioRouter`.
- Provide the single-shot :meth:`synthesize_bypass` path used by
  greeting / opt-out announcements.
- Track the in-flight synthesis task so cancellation can target it.
- Reserve the future :meth:`_synthesize_sentences` private hook for
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
from collections.abc import Callable
from typing import TYPE_CHECKING

from easycat._bounded_queue import BoundedAudioQueue
from easycat._tts_synthesizer import TTSSynthesizer
from easycat.cancel import CancelToken
from easycat.events import EventBus
from easycat.llm_output_processing import (
    LLMOutputProcessor,
    apply_output_processors,
)
from easycat.runtime.context import RunContext
from easycat.session._journal_sink import SessionJournalSink
from easycat.stages.tts import TTSStage
from easycat.timeouts import TimeoutConfig
from easycat.tts.input import TTSInput, strip_ssml_tags
from easycat.turn_manager import TurnManager

if TYPE_CHECKING:
    from easycat._turn_context import TurnContext
    from easycat.session._audio_router import AudioRouter
    from easycat.session._wiring import SessionWiringContext

logger = logging.getLogger(__name__)


class TTSScheduler:
    """Prepares and synthesizes TTS payloads for a Session."""

    def __init__(
        self,
        *,
        wiring: SessionWiringContext,
        tts_stage: TTSStage,
        turn_manager: TurnManager,
        event_bus: EventBus,
        journal_sink: SessionJournalSink,
        run_ctx: RunContext,
        no_turn: TurnContext,
        audio_router: AudioRouter,
        outbound_queue: BoundedAudioQueue,
        timeout_config: TimeoutConfig | None,
        audio_gate: Callable[[], bool] | None,
        # Config
        output_processors: list[LLMOutputProcessor],
        strip_markdown_enabled: bool,
    ) -> None:
        self._tts_getter = wiring.tts
        self._tts_stage = tts_stage
        self._turn_manager = turn_manager
        self._event_bus = event_bus
        self._journal_sink = journal_sink
        self._run_ctx = run_ctx
        self._no_turn = no_turn
        self._audio_router = audio_router

        self._output_processors = output_processors
        self._strip_markdown = strip_markdown_enabled

        self._current_turn = wiring.current_turn
        self._is_gated = wiring.is_gated
        self._drain_session_actions = wiring.drain_session_actions
        self._clear_turn = wiring.clear_turn

        self._synth = TTSSynthesizer(
            tts=wiring.tts(),
            event_bus=event_bus,
            outbound_queue=outbound_queue,
            timeout_config=timeout_config,
            correlation_ids=wiring.correlation_ids,
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

    async def finalize_speaking_turn(
        self,
        turn: TurnContext | None,
        *,
        turn_generation: int | None = None,
    ) -> bool:
        """Run the end-of-turn drain → stop → drain → clear sequence.

        The single owner of the bot-speaking stop / turn-clear decision:
        the streaming agent path (``TurnRunner._process_tts``) calls this
        so the barge-in / clear semantics live in exactly one place.

        Drains pending session actions (end_call, transfer) *before*
        transitioning the turn manager to IDLE so no new turn can sneak
        in. When ``should_stop`` is signalled the outbound audio is
        drained before stopping; otherwise the manager stops first and the
        queued tail audio is drained afterwards so the router can still
        record sent bytes and emit playback marks.

        The turn pointer is only cleared when the same turn (matched by
        identity *and*, when supplied, ``turn_generation``) is still
        active — a turn that was replaced (barge-in) or replaced-then-
        reissued under the same identity must not be cleared here.

        Returns ``True`` if a drained session action signalled that the
        session should stop.
        """
        should_stop = await self._drain_session_actions()
        if should_stop:
            await self._audio_router.await_drain()
            await self._turn_manager.bot_stopped_speaking()
        else:
            await self._turn_manager.bot_stopped_speaking()
            await self._audio_router.await_drain()
        if self._current_turn() is turn and (
            turn_generation is None or (turn is not None and turn.generation == turn_generation)
        ):
            self._clear_turn()
        return should_stop

    async def synthesize_bypass(self, text: str) -> None:
        """Synthesize text via TTS, bypassing the classification gate.

        Used for hold audio and screening responses that must reach the
        transport even while the gate is closed.
        """
        await self._synth.synthesize(text, token=None, bypass_gate=True)

    async def _synthesize_sentences(
        self,
        payloads: object,
        cancel_token: CancelToken | None,
        turn: TurnContext,
    ) -> object:
        """Reserved private placeholder for sentence-level pipelining.

        Not yet implemented — current behaviour is one-at-a-time synthesis
        driven by ``TurnRunner._process_tts`` via ``synthesizer.synthesize``.
        Kept private so the scheduler's public surface only advertises
        implemented methods; the hook exists so the pipelining change is a
        local one when it lands (see workstream-tts-pipelining).
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
