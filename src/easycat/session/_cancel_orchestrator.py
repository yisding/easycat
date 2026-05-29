"""Owns control-signal propagation and barge-in policy for a Session.

Responsibilities:

- Walk a control signal through every stage in late-to-early order
  (transport -> tts -> agent -> turn -> stt -> vad -> audio), giving each
  stage a chance to observe and record the signal via
  ``Stage.handle_upstream``.
- Compose ``STTCommitter.cancel()`` and ``TTSScheduler.cancel()``
  during a turn cancel (Session drives the composition; the
  orchestrator owns the signal-propagation tail of that path).
- Implement the barge-in suppression policy: if a queued session
  action declares ``no_interrupt=True`` (e.g. an end-call
  announcement), barge-in is suppressed and the orchestrator returns
  ``False`` so the TurnManager does not start a new user turn.
- Write the ``assistant_interruption_notified`` journal record so a
  bundle reader can reconstruct what text the user heard at the
  point of barge-in.

The orchestrator does not own the in-progress turn; that lives on
Session and is passed in via the ``current_turn`` callback.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from easycat.runtime.context import RunContext
from easycat.runtime.records import JournalRecordKind
from easycat.session._journal_sink import SessionJournalSink
from easycat.stages.base import (
    ControlSignal as _ControlSignal,
)
from easycat.stages.base import journal_append_control_signal as _journal_control_signal

if TYPE_CHECKING:
    from easycat.session._turn_context import TurnContext
    from easycat.session.actions import SessionActions
    from easycat.stages.base import Stage

logger = logging.getLogger(__name__)


class CancelOrchestrator:
    """Coordinates barge-in, control-signal propagation, and interruption records."""

    def __init__(
        self,
        *,
        # All 7 stages in propagation order (late -> early)
        transport_stage: Stage,
        tts_stage: Stage,
        agent_stage: Stage,
        turn_stage: Stage,
        stt_stage: Stage,
        vad_stage: Stage,
        audio_stage: Stage,
        # Context
        run_ctx: RunContext,
        journal_sink: SessionJournalSink,
        # Interruption config
        interruption_mode: str,
        interruption_latency_compensation_ms: int,
        interruption_ack_stale_ms: int,
        interruption_ack_tail_cap_ms: int,
        # Callbacks
        current_turn: Callable[[], TurnContext | None],
        session_actions: Callable[[], SessionActions | None],
        telephony_helpers_present: Callable[[], bool],
        cancel_turn_impl: Callable[..., Awaitable[None]],
    ) -> None:
        self._stages: tuple[Stage, ...] = (
            transport_stage,
            tts_stage,
            agent_stage,
            turn_stage,
            stt_stage,
            vad_stage,
            audio_stage,
        )
        self._run_ctx = run_ctx
        self._journal_sink = journal_sink

        self._interruption_mode = interruption_mode
        self._latency_compensation_ms = max(0, interruption_latency_compensation_ms)
        self._ack_stale_ms = max(0, interruption_ack_stale_ms)
        self._ack_tail_cap_ms = max(0, interruption_ack_tail_cap_ms)

        self._current_turn = current_turn
        self._session_actions = session_actions
        self._telephony_helpers_present = telephony_helpers_present
        self._cancel_turn_impl = cancel_turn_impl

    # ── Read-only config accessors ─────────────────────────────

    @property
    def interruption_mode(self) -> str:
        return self._interruption_mode

    @property
    def latency_compensation_ms(self) -> int:
        return self._latency_compensation_ms

    @property
    def ack_stale_ms(self) -> int:
        return self._ack_stale_ms

    @property
    def ack_tail_cap_ms(self) -> int:
        return self._ack_tail_cap_ms

    # ── Public API ─────────────────────────────────────────────

    async def propagate_signal(
        self,
        signal: _ControlSignal,
        *,
        cause: str | None = None,
    ) -> None:
        """Walk the upstream signal through every stage, late -> early.

        WS3 T3.8: control signals propagate from late stages (TTS,
        Transport) back toward early stages (VAD, STT) so each one can
        observe the event in journal order.  Each stage's
        ``handle_upstream`` writes a ``ControlSignalRecord`` so a replay
        can see who saw the signal and when.

        Errors inside ``handle_upstream`` are isolated per-stage --
        signal propagation must not throw and break the legacy cancel
        path that the same caller relies on.
        """
        for stage in self._stages:
            if stage is None:
                continue
            try:
                await stage.handle_upstream(signal, self._run_ctx)
            except Exception:  # noqa: BLE001 - never break cancel path
                logger.exception("Stage %s.handle_upstream failed", stage.name)
        # Telephony helpers journal the signal without a dedicated stage
        # wrapper: one bare aggregate control-signal record keeps the
        # observability identical to the old stage path.
        if self._telephony_helpers_present():
            _journal_control_signal(self._run_ctx, stage="telephony", signal=signal)
        # Annotate the trailing signal record with the originating cause
        # so the replay UI can display "interrupt -- barge_in" instead of
        # bare signal IDs.
        if cause:
            turn = self._current_turn()
            self._journal_sink.append_record(
                kind=JournalRecordKind.CONTROL,
                name="control_signal_cause",
                turn_id=turn.id if turn else None,
                data={"signal_id": signal.signal_id, "cause": cause},
            )

    async def for_barge_in(self) -> bool:
        """Cancel current turn due to barge-in (called by TurnManager).

        Returns ``False`` when barge-in is suppressed so the TurnManager
        skips starting a new user turn.

        When a queued session action has ``no_interrupt=True`` (e.g. an
        end-call or transfer announcement), barge-in is
        suppressed so the critical speech plays in full.
        """
        actions = self._session_actions()
        if actions is not None and actions.no_interrupt:
            logger.debug("Barge-in suppressed: queued action has no_interrupt=True")
            return False
        await self._cancel_turn_impl(barge_in=True)
        return True

    def record_interruption(
        self,
        *,
        source: str,
        mode: str,
        text_spoken: str,
        notified: bool,
        turn_id: str | None = None,
    ) -> None:
        """Write the ``assistant_interruption_notified`` journal record."""
        self._journal_sink.append_record(
            name="assistant_interruption_notified",
            turn_id=turn_id,
            data={
                "source": source,
                "mode": mode,
                "text_spoken": text_spoken,
                "notified": notified,
            },
        )
