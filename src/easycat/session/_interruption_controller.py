"""InterruptionController — owns the seven-step interruption flow."""

from __future__ import annotations

import logging
from typing import Any

from easycat.integrations.agents.base import CancellationMode, ShallowModeInterruptionError
from easycat.runtime.records import JournalRecordKind

logger = logging.getLogger(__name__)


class InterruptionController:
    """Owns the seven-step interruption flow from WS2B.

    Steps:
        1. detect — caller detects the interruption (already done).
        2. signal — emit a ControlSignalRecord via the journal.
        3. measure — determine how much text was delivered.
        4. select — pick the cancellation mode.
        5. mutate — call ``bridge.apply_interruption``.
        6. observe — handle ``ShallowModeInterruptionError``.
        7. transition — update turn state.
    """

    def __init__(self, *, journal: Any = None) -> None:
        self._journal = journal
        self._pending_downgrade = False

    def signal_interrupt(
        self,
        cause: str,
        *,
        delivered_text: str = "",
        bridge: Any = None,
        recorder: Any = None,
        mode: CancellationMode | str | None = None,
    ) -> None:
        """Execute the interruption flow.

        Parameters
        ----------
        cause:
            Why the interruption occurred (e.g. "barge_in", "timeout").
        delivered_text:
            The text estimated to have been heard by the user.
        bridge:
            An agent bridge with ``apply_interruption`` (optional).
        recorder:
            Journal or recorder for audit trail (optional).
        mode:
            Cancellation mode override. Accepts a ``CancellationMode`` enum
            or a string value (resolved via the enum).
        """
        # Step 2: signal
        self._record_signal(cause)

        # Step 3: measure delivered text (caller provides it)

        # Step 4: select cancellation mode
        effective_mode = self._resolve_mode(mode)

        # Step 5: mutate
        if bridge is not None and hasattr(bridge, "apply_interruption"):
            try:
                bridge.apply_interruption(
                    delivered_text=delivered_text,
                    mode=effective_mode,
                    recorder=recorder,
                )
            except ShallowModeInterruptionError:
                # Step 6a: observe — shallow workflow cannot be interrupted
                # mid-turn.  Downgrade to end-of-turn interruption and
                # record the downgrade in the journal so replay/doctor
                # can surface it.
                self._pending_downgrade = True
                self._record_signal("shallow_mode_downgrade")
                logger.warning(
                    "Shallow-mode workflow does not support mid-turn "
                    "interruption; downgrading to end-of-turn interruption. "
                    "Convert to deep mode or implement "
                    "workflow.apply_interruption() to enable barge-in."
                )
            except Exception:
                # Step 6b: observe — handle other failures gracefully
                logger.debug("apply_interruption failed for cause=%s", cause, exc_info=True)
                self._pending_downgrade = True

        # Step 7: transition (caller manages TurnManager state)

    def signal_text_interrupt(self, new_text: str) -> None:
        """Handle concurrent ``send_text`` as an interruption."""
        self._record_signal(f"text_interrupt:{new_text[:40]}")

    @staticmethod
    def _resolve_mode(mode: CancellationMode | str | None) -> CancellationMode:
        if isinstance(mode, CancellationMode):
            return mode
        if isinstance(mode, str):
            try:
                return CancellationMode(mode)
            except ValueError:
                return CancellationMode.IMMEDIATE_STOP
        return CancellationMode.IMMEDIATE_STOP

    def _record_signal(self, cause: str) -> None:
        if self._journal is not None:
            self._journal.append(
                kind=JournalRecordKind.CONTROL,
                name="interruption_signal",
                session_id="",
                data={"cause": cause},
            )
