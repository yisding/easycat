"""Runtime-owned background task scope."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from easycat.session._journal_sink import SessionJournalSink

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class RuntimeScope:
    """Track named runtime tasks and provide consistent cancellation/drain."""

    def __init__(self) -> None:
        self._tasks: dict[str, set[asyncio.Task[Any]]] = {}

    def create_task(
        self,
        name: str,
        coro: Coroutine[Any, Any, _T],
        *,
        task_name: str | None = None,
    ) -> asyncio.Task[_T]:
        """Create and track a named task."""
        task = asyncio.create_task(coro, name=task_name or name)
        return self.add_task(name, task)

    def create_journaled_task(
        self,
        coro: Coroutine[Any, Any, _T],
        *,
        name: str,
        journal_sink: SessionJournalSink,
        turn_id: str | None = None,
    ) -> asyncio.Task[_T]:
        """Create a tracked task that journals scheduled/completed/cancelled/raised.

        Emits ``task_scheduled`` at creation, then one of
        ``task_completed`` / ``task_cancelled`` / ``task_raised`` when
        the task finishes.  A bundle reader can reconstruct a Gantt
        chart of concurrent awaits — enough to diagnose races like the
        plan-7 STT-commit-vs-end-stream interleave without re-running
        the live providers.

        *name* is the stable label that survives replay (e.g.
        ``"stt_pause_commit"``, ``"tts_synth"``, ``"on_turn_ended"``).
        Use one per logical task — don't baseline it on Python object
        ids, which don't survive serialisation.
        """
        # Resolve the turn id once at scheduling time so the terminal
        # record carries the same id even if a new turn has started by
        # the time the task completes.
        resolved_turn = journal_sink.current_turn_id(turn_id)
        journal_sink.append_record(
            name="task_scheduled",
            turn_id=resolved_turn,
            data={"task_name": name},
        )
        task = asyncio.create_task(coro, name=name)

        def _on_done(
            t: asyncio.Task[Any],
            label: str = name,
            tid: str | None = resolved_turn,
        ) -> None:
            # Pick the right terminal record kind.  A cancelled task is
            # reported as ``task_cancelled`` even if it also raised during
            # finally-cleanup: ``t.cancelled()`` is checked first and
            # short-circuits before ``t.exception()`` is consulted.
            try:
                if t.cancelled():
                    journal_sink.append_record(
                        name="task_cancelled", turn_id=tid, data={"task_name": label}
                    )
                    return
                exc = t.exception()
            except asyncio.CancelledError:
                journal_sink.append_record(
                    name="task_cancelled", turn_id=tid, data={"task_name": label}
                )
                return
            if exc is not None:
                journal_sink.append_record(
                    name="task_raised",
                    turn_id=tid,
                    data={"task_name": label, "exc_type": type(exc).__name__},
                )
            else:
                journal_sink.append_record(
                    name="task_completed", turn_id=tid, data={"task_name": label}
                )

        task.add_done_callback(_on_done)
        return self.add_task(name, task)

    @staticmethod
    def log_task_exception(task: asyncio.Task[object]) -> None:
        """Done-callback that logs an unhandled task exception.

        Pair with :meth:`create_journaled_task`: the journal records the
        terminal record for bundles; this surfaces the traceback in logs.
        """
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Background task failed")

    def add_task(self, name: str, task: asyncio.Task[_T]) -> asyncio.Task[_T]:
        """Track an existing task under *name*.

        Adding under a name purges previously-tracked tasks for that
        same name that have already completed, so reusing a name
        (e.g. per-segment commit tasks) does not accumulate dead
        entries between drains. Pending tasks are left in place —
        call :meth:`drain` to observe their results and clear them.
        """
        if not name:
            raise ValueError("RuntimeScope task name must be non-empty")

        bucket = self._tasks.setdefault(name, set())
        bucket.difference_update({existing for existing in bucket if existing.done()})
        bucket.add(task)
        return task

    def tasks(self, name: str | None = None) -> tuple[asyncio.Task[Any], ...]:
        """Return tracked tasks that have not been drained yet."""
        if name is not None:
            return tuple(self._tasks.get(name, ()))
        return tuple(task for tasks in self._tasks.values() for task in tasks)

    @property
    def empty(self) -> bool:
        """Whether the scope has no pending tracked tasks."""
        return not self.tasks()

    def cancel(self, name: str | None = None) -> tuple[asyncio.Task[Any], ...]:
        """Cancel pending tasks and return the tasks that were targeted."""
        tasks = self.tasks(name)
        for task in tasks:
            if not task.done():
                task.cancel()
        return tasks

    async def drain(self, name: str | None = None, *, cancel: bool = False) -> None:
        """Wait for pending tasks to finish, optionally cancelling them first.

        Every snapshotted task is awaited and discarded even if one of
        them fails; the first observed exception (if any) is re-raised
        once the drain completes, so callers cannot silently leave
        sibling tasks pending. When *cancel* is True, expected
        cancellation/exception teardown is swallowed.
        """
        tasks = self.cancel(name) if cancel else self.tasks(name)
        current = asyncio.current_task()
        pending: BaseException | None = None

        for task in tasks:
            if task is current:
                continue
            try:
                await task
            except asyncio.CancelledError as exc:
                if not cancel and pending is None:
                    pending = exc
            except Exception as exc:
                if not cancel and pending is None:
                    pending = exc
            finally:
                self._discard_task(task)

        if pending is not None:
            raise pending

    async def cancel_and_drain(self, name: str | None = None) -> None:
        """Cancel pending tasks, then wait for cancellation cleanup to finish."""
        await self.drain(name, cancel=True)

    def _discard_task(self, task: asyncio.Task[Any]) -> None:
        for name, tasks in tuple(self._tasks.items()):
            if task in tasks:
                tasks.discard(task)
                if not tasks:
                    self._tasks.pop(name, None)
                return
