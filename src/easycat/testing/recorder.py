"""In-memory :class:`AgentRecorder` double for agent-bridge contract tests.

Bridges journal their execution (unit cursors, tool calls, handoffs, state
snapshots, interruption boundaries) through the ``AgentRecorder`` protocol.
:class:`RecordingAgentRecorder` captures every write as a plain tuple so a
contract test can assert *what* a bridge journaled without standing up the
real :mod:`easycat.runtime` journal.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from easycat.integrations.agents.base import (
    CancellationMode,
    ExecutionCursor,
    RecorderContext,
)
from easycat.runtime.records import ErrorInfo

__all__ = ["RecordingAgentRecorder"]


class RecordingAgentRecorder:
    """Structurally satisfies ``AgentRecorder`` and remembers every write.

    Each journal write is appended to :attr:`records` as a
    ``(kind, args, kwargs)`` tuple, where ``kind`` is the record method name
    minus its ``record_`` prefix (``"unit_entered"``, ``"tool_call"``,
    ``"cancellation_boundary"``, ...). Use :meth:`kinds` for order-sensitive
    assertions and :meth:`tool_phases` for the tool-call phase sequence.
    """

    def __init__(self) -> None:
        self.context = RecorderContext(run_id="run-1", session_id="session-1", turn_id="turn-1")
        self.records: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def kinds(self) -> list[str]:
        """Return the record kinds in write order."""
        return [record[0] for record in self.records]

    def tool_phases(self) -> list[str]:
        """Return the phase of every ``tool_call`` record in write order."""
        return [record[1][0] for record in self.records if record[0] == "tool_call"]

    def record_unit_entered(self, cursor: ExecutionCursor) -> None:
        self.records.append(("unit_entered", (cursor,), {}))

    def record_unit_exited(self, cursor: ExecutionCursor, reason: str | None = None) -> None:
        self.records.append(("unit_exited", (cursor,), {"reason": reason}))

    def safe_exit_cursor(self, cursor: ExecutionCursor, reason: str | None = "error") -> None:
        try:
            self.record_unit_exited(cursor, reason=reason)
        except Exception:
            pass

    @contextmanager
    def unit(
        self,
        cursor: ExecutionCursor,
        *,
        commit_on_exit: bool = True,
    ) -> Iterator[ExecutionCursor]:
        del commit_on_exit
        self.record_unit_entered(cursor)
        try:
            yield cursor
        finally:
            self.record_unit_exited(cursor)

    def record_tool_call(
        self,
        phase: str,
        name: str,
        args_ref: str | None = None,
        result_ref: str | None = None,
        call_id: str | None = None,
    ) -> None:
        self.records.append(
            (
                "tool_call",
                (phase, name),
                {"args_ref": args_ref, "result_ref": result_ref, "call_id": call_id},
            )
        )

    def record_state_snapshot(self, ref: str, *, payload: bytes | None = None) -> str:
        self.records.append(("state_snapshot", (ref,), {"payload": payload}))
        return ref

    def record_framework_handoff(
        self,
        from_unit: str | None,
        to_unit: str,
        reason: str | None = None,
    ) -> None:
        self.records.append(("handoff", (from_unit, to_unit), {"reason": reason}))

    def record_cancellation_boundary(
        self,
        mode: CancellationMode,
        reason: str | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        self.records.append(
            (
                "cancellation_boundary",
                (mode,),
                {"reason": reason, "caused_by_signal_id": caused_by_signal_id},
            )
        )

    def record_framework_error(self, error: ErrorInfo) -> None:
        self.records.append(("framework_error", (error,), {}))

    def record_state_committed(
        self,
        mutation_kind: str,
        pre_state_ref: str | None = None,
        post_state_ref: str | None = None,
    ) -> None:
        self.records.append(
            (
                "state_committed",
                (mutation_kind,),
                {"pre_state_ref": pre_state_ref, "post_state_ref": post_state_ref},
            )
        )

    def record_interruption_apply_failed(
        self,
        mutation_kind: str,
        pre_state_ref: str | None = None,
        post_state_ref: str | None = None,
        failure_error: ErrorInfo | None = None,
    ) -> None:
        self.records.append(
            (
                "interruption_apply_failed",
                (mutation_kind,),
                {
                    "pre_state_ref": pre_state_ref,
                    "post_state_ref": post_state_ref,
                    "failure_error": failure_error,
                },
            )
        )
