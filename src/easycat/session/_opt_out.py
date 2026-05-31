"""TCPA opt-out auto-detection for a Session.

Runs on every STT final.  On a phrase match it emits
:class:`~easycat.events.OptOutDetected`, adds the caller's number to an
attached Do-Not-Call list, and queues ``EndCallAction(reason="opt_out")``
so the call hangs up after the current agent utterance.  When no action
queue is present it falls back to scheduling ``Session.stop()`` — opt-out
must terminate the call.

The policy reads the caller number via the *raw* identity accessor
(``CallerIdState.private_identity``) so even ``"off"`` caller-ID exposure
still feeds DNC state.  Disable via ``SessionConfig.opt_out_detection``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from easycat.events import EventBus, STTFinal
from easycat.runtime.scope import RuntimeScope
from easycat.session._journal_sink import SessionJournalSink

if TYPE_CHECKING:
    from easycat.session._caller_id import CallerIdState
    from easycat.session.actions import SessionActions

logger = logging.getLogger(__name__)


class OptOutPolicy:
    """Detects opt-out phrases in STT finals and triggers DNC + hangup."""

    def __init__(
        self,
        *,
        enabled: bool,
        phrases: list[str] | None,
        dnc_list: Any | None,
        caller_id: CallerIdState,
        session_actions: Callable[[], SessionActions | None],
        emit: Callable[[Any], Awaitable[None]],
        stop: Callable[[], Awaitable[None]],
        event_bus: EventBus,
        runtime_scope: RuntimeScope,
        journal_sink: SessionJournalSink,
    ) -> None:
        self._enabled = enabled
        self._phrases = phrases
        self._dnc_list = dnc_list
        self._caller_id = caller_id
        self._session_actions = session_actions
        self._emit = emit
        self._stop = stop
        self._event_bus = event_bus
        self._runtime_scope = runtime_scope
        self._journal_sink = journal_sink

        if self._enabled:
            self._event_bus.subscribe(STTFinal, self._on_stt_final)

    # ── DNC list (delegated by Session.dnc_list) ─────────────────

    @property
    def dnc_list(self) -> Any | None:
        return self._dnc_list

    @dnc_list.setter
    def dnc_list(self, value: Any | None) -> None:
        self._dnc_list = value

    # ── Event handler ────────────────────────────────────────────

    async def _on_stt_final(self, event: STTFinal) -> None:
        """Detect opt-out phrases in every STT final and react.

        Skipped when the text is empty.  On match: emits
        :class:`~easycat.events.OptOutDetected`, adds the caller to the
        DNC list if set, and enqueues a :class:`EndCallAction` so the
        call terminates after the current agent utterance.
        """
        from easycat.events import OptOutDetected
        from easycat.telephony.compliance import match_opt_out_phrase

        if not event.text:
            return
        phrase = match_opt_out_phrase(event.text, self._phrases)
        if phrase is None:
            return

        identity = self._caller_id.private_identity
        number = identity.caller_number if identity else ""
        if self._dnc_list is not None and number:
            try:
                self._dnc_list.add(number)
            except Exception:
                logger.debug("dnc_list.add raised for opt-out", exc_info=True)

        await self._emit(OptOutDetected(number=number, phrase=phrase, text=event.text))

        # Queue a hangup after the current agent utterance so the session
        # drains cleanly (saying "understood, goodbye" first is the
        # agent's job; we just schedule the end).  When no action queue is
        # present we fall back to firing ``Session.stop`` — opt-out must
        # terminate the call.
        actions = self._session_actions()
        if actions is not None:
            actions.end_call(reason="opt_out")
        else:
            self._runtime_scope.create_journaled_task(
                self._stop(),
                name="opt_out_stop",
                journal_sink=self._journal_sink,
            )


__all__ = ["OptOutPolicy"]
