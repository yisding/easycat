"""Session cost-budget enforcement."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from easycat.runtime.records import JournalRecordKind
from easycat.runtime.scope import RuntimeScope

logger = logging.getLogger(__name__)


class JournalSink(Protocol):
    """Journal surface needed for cost-budget enforcement records."""

    def current_turn_id(self, turn_id: str | None = None) -> str | None: ...

    def append_record(
        self,
        *,
        name: str,
        kind: JournalRecordKind = JournalRecordKind.EVENT,
        turn_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None: ...


class StopSession(Protocol):
    """Session stop callable, typed around Session.stop's keyword-only force flag."""

    def __call__(self, *, force: bool = False) -> Awaitable[None]: ...


@dataclass(slots=True)
class CostBudgetEnforcer:
    """Schedule session teardown after the cost budget is exceeded."""

    session_id: str
    runtime_scope: RuntimeScope
    journal_sink: JournalSink
    stop_session: StopSession
    is_closed: Callable[[], bool]
    is_stopping: Callable[[], bool]
    _stop_requested: bool = field(default=False, init=False)

    def on_exceeded(self, alert: dict[str, Any], turn_id: str | None) -> None:
        """Record and schedule the one-shot budget stop request."""
        if self.is_closed() or self.is_stopping() or self._stop_requested:
            return
        self._stop_requested = True
        self.journal_sink.append_record(
            kind=JournalRecordKind.CONTROL,
            name="cost_budget_stop_requested",
            turn_id=turn_id,
            data={
                "reason": "max_session_cost_usd_exceeded",
                "budget_status": alert.get("budget_status"),
                "total_usd": alert.get("total_usd"),
                "max_session_cost_usd": alert.get("max_session_cost_usd"),
                "overage_usd": alert.get("overage_usd"),
                "trigger_record_name": alert.get("trigger_record_name"),
            },
        )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "Cost budget exceeded for session %s outside a running event loop; "
                "stop(force=True) could not be scheduled.",
                self.session_id,
            )
            return
        task = self.runtime_scope.create_journaled_task(
            self._stop_for_cost_budget(alert),
            name="cost_budget_stop",
            journal_sink=self.journal_sink,
            turn_id=turn_id,
        )
        task.add_done_callback(self.runtime_scope.log_task_exception)

    async def _stop_for_cost_budget(self, alert: dict[str, Any]) -> None:
        """Force-stop the session from a runtime-scope task after budget breach."""
        current = asyncio.current_task()
        if current is not None:
            self.runtime_scope.discard(current)
        if self.is_closed() or self.is_stopping():
            return
        logger.warning(
            "Stopping session %s because max_session_cost_usd=%r was exceeded (total_usd=%r).",
            self.session_id,
            alert.get("max_session_cost_usd"),
            alert.get("total_usd"),
        )
        await self.stop_session(force=True)
