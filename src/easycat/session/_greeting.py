"""Optional "bot speaks first" greeting for a Session.

When a :class:`~easycat.events.CallAnswered` event fires (inbound stream
start or outbound callee pick-up), the controller synthesizes the
configured greeting once via the bypass path so it plays even while a
classification gate is still buffering (the outbound answering-machine
window).  Only the first occurrence is honoured, so a warm-transfer
re-answer (a second ``CallAnswered``) does not re-greet.

The controller subscribes itself to the event bus and owns the greeting
task; Session's ``stop()`` delegates the task cancel to :meth:`cancel`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from easycat.events import CallAnswered, EventBus
from easycat.runtime.scope import RuntimeScope
from easycat.session._journal_sink import SessionJournalSink

logger = logging.getLogger(__name__)


class GreetingController:
    """Synthesizes the configured greeting once on the first CallAnswered."""

    def __init__(
        self,
        *,
        greeting: str | None,
        synthesize: Callable[[str], Awaitable[None]],
        event_bus: EventBus,
        runtime_scope: RuntimeScope,
        journal_sink: SessionJournalSink,
    ) -> None:
        self._greeting = greeting
        self._synthesize = synthesize
        self._event_bus = event_bus
        self._runtime_scope = runtime_scope
        self._journal_sink = journal_sink

        self._spoken: bool = False
        self._task: asyncio.Task[Any] | None = None

        if self._greeting:
            self._event_bus.subscribe(CallAnswered, self._on_call_answered)

    # ── Introspection (used by Session teardown / tests) ─────────

    @property
    def task(self) -> asyncio.Task[Any] | None:
        return self._task

    @property
    def spoken(self) -> bool:
        return self._spoken

    # ── Event handler ────────────────────────────────────────────

    def _on_call_answered(self, event: Any) -> None:
        """Schedule the configured greeting once per call.

        The actual TTS work is detached from event dispatch so outbound
        status callbacks can complete lifecycle/AMD processing without
        waiting on synthesis.  Subsequent ``CallAnswered`` events — e.g.
        a warm-transfer re-answer — are ignored.
        """
        if self._spoken or not self._greeting:
            return
        if self._task is not None and not self._task.done():
            return
        task = self._runtime_scope.create_journaled_task(
            self._deliver(self._greeting),
            name="call_answered_greeting",
            journal_sink=self._journal_sink,
        )
        self._task = task
        task.add_done_callback(self._clear_task)

    def _clear_task(self, task: asyncio.Task[Any]) -> None:
        if self._task is task:
            self._task = None

    async def _deliver(self, greeting: str) -> None:
        await asyncio.sleep(0)
        try:
            await self._synthesize(greeting)
            self._spoken = True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Failed to synthesize greeting", exc_info=True)

    # ── Cancellation ─────────────────────────────────────────────

    async def cancel(self) -> None:
        """Drain the in-flight greeting task (graceful stop path)."""
        task = self._task
        if task is not None and not task.done():
            await self._runtime_scope.cancel_and_drain("call_answered_greeting")
        self._task = None

    def clear_task(self) -> None:
        """Drop the task handle without awaiting (force stop path)."""
        self._task = None


__all__ = ["GreetingController"]
