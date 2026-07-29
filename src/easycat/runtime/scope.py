"""Runtime-owned background task scope."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from functools import partial
from typing import Any, Protocol, TypeVar, runtime_checkable

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class BackgroundTaskScope:
    """Own self-pruning, fire-and-forget tasks for synchronous components.

    Unlike :class:`RuntimeScope`, this scope has no async drain boundary. It
    retains each task until completion, consumes its terminal result, and
    removes it automatically. Named tasks can be replaced or cancelled while
    the owning component keeps a synchronous ``stop()`` contract.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    def create_task(
        self,
        name: str,
        coro: Coroutine[Any, Any, _T],
        *,
        replace: bool = False,
    ) -> asyncio.Task[_T]:
        """Create a named task, optionally cancelling an active predecessor."""
        if not name:
            coro.close()
            raise ValueError("BackgroundTaskScope task name must be non-empty")

        existing = self._tasks.get(name)
        if existing is not None and not existing.done():
            if not replace:
                coro.close()
                raise RuntimeError(f"Background task {name!r} is already active")
            self.cancel(name)

        try:
            task = asyncio.create_task(coro, name=name)
        except BaseException:
            coro.close()
            raise
        self._tasks[name] = task
        task.add_done_callback(partial(self._on_done, name))
        return task

    def active(self, name: str) -> bool:
        """Return whether *name* currently maps to an unfinished task."""
        task = self._tasks.get(name)
        return task is not None and not task.done()

    def tasks(self) -> tuple[asyncio.Task[Any], ...]:
        """Return the tasks that are still owned by this scope."""
        return tuple(task for task in self._tasks.values() if not task.done())

    @property
    def empty(self) -> bool:
        """Whether the scope owns no unfinished tasks."""
        return not self.tasks()

    def cancel(self, name: str | None = None) -> tuple[asyncio.Task[Any], ...]:
        """Detach and cancel one named task or every task in the scope.

        The calling task is detached but not cancelled when it belongs to this
        scope, allowing event callbacks triggered by that task to tear down the
        owner without interrupting their own cleanup.
        """
        if name is None:
            tasks = tuple(self._tasks.values())
            self._tasks.clear()
        else:
            task = self._tasks.pop(name, None)
            tasks = () if task is None else (task,)

        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        for task in tasks:
            if task is not current and not task.done():
                task.cancel()
        return tasks

    def _on_done(self, name: str, task: asyncio.Task[Any]) -> None:
        if self._tasks.get(name) is task:
            self._tasks.pop(name, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Background task %r failed", name)


@runtime_checkable
class JournalSink(Protocol):
    """Minimal structural sink that :meth:`RuntimeScope.create_journaled_task` needs.

    Defined in the runtime layer so task plumbing depends only on its own
    abstractions. Concrete sinks (e.g. the session package's
    ``SessionJournalSink``) satisfy this protocol structurally.
    """

    def current_turn_id(self, turn_id: str | None = ...) -> str | None:
        """Resolve the turn id to record, defaulting to the active turn."""
        ...

    def append_record(
        self,
        *,
        name: str,
        turn_id: str | None = ...,
        data: dict[str, Any] | None = ...,
    ) -> int | None:
        """Append a journal record."""
        ...


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
        journal_sink: JournalSink,
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
        """Cancel pending tasks and return the tasks that were targeted.

        When the caller itself belongs to this scope, detach it without
        cancelling it.  Runtime-owned event callbacks may legitimately tear
        down their owner; self-cancellation would otherwise interrupt that
        teardown at its next suspension point.  Sibling tasks are still
        cancelled normally.
        """
        tasks = self.tasks(name)
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        for task in tasks:
            if task is current:
                self._discard_task(task)
            elif not task.done():
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
            cancellation_requests = current.cancelling() if current is not None else 0
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if current is not None and current.cancelling() > cancellation_requests:
                    raise
                if not cancel and pending is None:
                    pending = exc
            except Exception as exc:
                if not cancel and pending is None:
                    pending = exc
            finally:
                if task.done():
                    self._discard_task(task)

        if pending is not None:
            raise pending

    async def cancel_and_drain(self, name: str | None = None) -> None:
        """Cancel pending tasks, then wait for cancellation cleanup to finish."""
        await self.drain(name, cancel=True)

    def discard(self, task: asyncio.Task[Any]) -> None:
        """Stop tracking *task* without awaiting it.

        Use this only when the current task is performing its own teardown
        and cannot safely await itself. The task's own done callbacks still
        run when it exits; this only removes the task from the scope's drain
        bookkeeping.
        """
        self._discard_task(task)

    def _discard_task(self, task: asyncio.Task[Any]) -> None:
        for name, tasks in tuple(self._tasks.items()):
            if task in tasks:
                tasks.discard(task)
                if not tasks:
                    self._tasks.pop(name, None)
                return
