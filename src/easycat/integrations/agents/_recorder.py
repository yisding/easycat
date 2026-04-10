"""Concrete AgentRecorder backed by ExecutionJournal + ArtifactStore.

Enforces basic invariants (paired enter/exit, no duplicate unit_ids)
and provides the ``unit()`` context manager for guaranteed cleanup.
All writes are no-ops when the journal is ``None``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Literal

from easycat.integrations.agents.base import (
    CancellationMode,
    ExecutionCursor,
    RecorderContext,
    RecorderInvariantError,
)
from easycat.runtime.records import ErrorInfo, JournalRecordKind

if TYPE_CHECKING:
    from easycat.runtime.artifacts import ArtifactStore
    from easycat.runtime.journal import ExecutionJournal

logger = logging.getLogger(__name__)


class JournalAgentRecorder:
    """Concrete ``AgentRecorder`` that writes to the WS1 journal.

    Bridges receive a fresh instance per ``invoke()`` call, bound to
    the current run/session/turn context.  All writes pass through the
    WS1 ``apply_write_filter`` hook.
    """

    def __init__(
        self,
        journal: ExecutionJournal | None,
        artifact_store: ArtifactStore | None,
        context: RecorderContext,
    ) -> None:
        self._journal = journal
        self._artifact_store = artifact_store
        self._context = context
        # Stack of open cursors for invariant enforcement.
        self._open_cursors: list[ExecutionCursor] = []
        self._seen_unit_ids: set[str] = set()

    @property
    def context(self) -> RecorderContext:
        return self._context

    # ── Unit lifecycle ───────────────────────────────────────────

    def record_unit_entered(self, cursor: ExecutionCursor) -> None:
        if cursor.unit_id in self._seen_unit_ids:
            raise RecorderInvariantError(f"Duplicate unit_id {cursor.unit_id!r} within this turn")
        self._seen_unit_ids.add(cursor.unit_id)
        self._open_cursors.append(cursor)
        self._append(
            kind=JournalRecordKind.FRAMEWORK_TRANSITION,
            name="unit_entered",
            data={
                "unit_id": cursor.unit_id,
                "unit_kind": (
                    cursor.unit_kind.value
                    if hasattr(cursor.unit_kind, "value")
                    else str(cursor.unit_kind)
                ),
                "display_name": cursor.display_name,
                "parent_unit_id": cursor.parent_unit_id,
                "committable": cursor.committable,
                "direction": "enter",
            },
        )

    def record_unit_exited(self, cursor: ExecutionCursor, reason: str | None = None) -> None:
        if not self._open_cursors:
            raise RecorderInvariantError(
                f"record_unit_exited({cursor.unit_id!r}) without a matching record_unit_entered"
            )
        top = self._open_cursors[-1]
        if top.unit_id != cursor.unit_id:
            raise RecorderInvariantError(
                f"record_unit_exited({cursor.unit_id!r}) but the top of the "
                f"cursor stack is {top.unit_id!r}"
            )
        self._open_cursors.pop()
        self._append(
            kind=JournalRecordKind.FRAMEWORK_TRANSITION,
            name="unit_exited",
            data={
                "unit_id": cursor.unit_id,
                "unit_kind": (
                    cursor.unit_kind.value
                    if hasattr(cursor.unit_kind, "value")
                    else str(cursor.unit_kind)
                ),
                "display_name": cursor.display_name,
                "committable": cursor.committable,
                "exit_reason": reason,
                "direction": "exit",
            },
        )

    @contextmanager
    def unit(
        self, cursor: ExecutionCursor, *, commit_on_exit: bool = True
    ) -> Iterator[ExecutionCursor]:
        """Context manager wrapping enter/exit with guaranteed cleanup."""
        self.record_unit_entered(cursor)
        try:
            yield cursor
        except BaseException as exc:
            self.record_unit_exited(cursor, reason=f"exception:{type(exc).__name__}")
            raise
        else:
            final = cursor.with_committable(True) if commit_on_exit else cursor
            self.record_unit_exited(final, reason=None)

    # ── Tool calls ───────────────────────────────────────────────

    def record_tool_call(
        self,
        phase: Literal["start", "delta", "result", "error"],
        name: str,
        args_ref: str | None = None,
        result_ref: str | None = None,
    ) -> None:
        self._append(
            kind=JournalRecordKind.FRAMEWORK_TRANSITION,
            name="tool_phase_changed",
            data={
                "phase": phase,
                "tool_name": name,
                "args_ref": args_ref,
                "result_ref": result_ref,
            },
        )

    # ── State snapshots ──────────────────────────────────────────

    def record_state_snapshot(self, ref: str, *, payload: bytes | None = None) -> None:
        if payload is not None and self._artifact_store is not None:
            ref = self._artifact_store.put(payload) or ref
        self._append(
            kind=JournalRecordKind.FRAMEWORK_TRANSITION,
            name="state_snapshot",
            data={"state_ref": ref},
        )

    # ── Framework handoffs ───────────────────────────────────────

    def record_framework_handoff(
        self,
        from_unit: str | None,
        to_unit: str,
        reason: str | None = None,
    ) -> None:
        self._append(
            kind=JournalRecordKind.FRAMEWORK_TRANSITION,
            name="framework_handoff",
            data={
                "from_unit": from_unit,
                "to_unit": to_unit,
                "handoff_reason": reason,
            },
        )

    # ── Cancellation boundaries ──────────────────────────────────

    def record_cancellation_boundary(
        self,
        mode: CancellationMode,
        reason: str | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        self._append(
            kind=JournalRecordKind.FRAMEWORK_TRANSITION,
            name="cancellation_boundary",
            data={
                "cancellation_mode": mode.value,
                "boundary_reason": reason,
                "caused_by_signal_id": caused_by_signal_id,
            },
        )

    # ── Framework errors ─────────────────────────────────────────

    def record_framework_error(self, error: ErrorInfo) -> None:
        self._append(
            kind=JournalRecordKind.FRAMEWORK_TRANSITION,
            name="framework_error",
            error=error,
        )

    # ── Atomic interruption records ─────────────────────────────

    def record_state_committed(
        self,
        mutation_kind: str,
        pre_state_ref: str | None = None,
        post_state_ref: str | None = None,
    ) -> None:
        self._append(
            kind=JournalRecordKind.FRAMEWORK_TRANSITION,
            name="state_committed",
            data={
                "mutation_kind": mutation_kind,
                "pre_state_ref": pre_state_ref,
                "post_state_ref": post_state_ref,
                "direction": "enter",
            },
        )

    def record_interruption_apply_failed(
        self,
        mutation_kind: str,
        pre_state_ref: str | None = None,
        post_state_ref: str | None = None,
        failure_error: ErrorInfo | None = None,
    ) -> None:
        self._append(
            kind=JournalRecordKind.FRAMEWORK_TRANSITION,
            name="interruption_apply_failed",
            data={
                "mutation_kind": mutation_kind,
                "pre_state_ref": pre_state_ref,
                "post_state_ref": post_state_ref,
            },
            error=failure_error,
        )

    # ── Internal journal write ───────────────────────────────────

    def _append(
        self,
        kind: JournalRecordKind,
        name: str,
        data: dict[str, Any] | None = None,
        error: ErrorInfo | None = None,
    ) -> None:
        if self._journal is None:
            return
        self._journal.append(
            kind=kind,
            name=name,
            session_id=self._context.session_id,
            turn_id=self._context.turn_id,
            data=_scrub_secrets(data) if data else data,
            error=error,
        )


def _scrub_secrets(data: dict[str, Any]) -> dict[str, Any]:
    """Remove data keys whose names match secret-adjacent fragments.

    Enforces the WS1 safe-default: no raw API keys, auth headers, or
    credentials reach the journal via bridge records.
    """
    from easycat.runtime.safe_defaults import _is_secret_name

    return {k: v for k, v in data.items() if not _is_secret_name(k)}
