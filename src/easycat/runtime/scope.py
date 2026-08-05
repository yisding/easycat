"""Runtime-owned background task scope."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from functools import partial
from typing import Any, Protocol, Self, TypeVar, runtime_checkable

from easycat._concurrency import (
    RuntimeSupervisor,
    SurvivorRegistry,
    checkpoint_pending_cancellation,
    start_owned,
)

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
    """Track named runtime tasks in an explicit lifecycle hierarchy.

    Legacy standalone scopes may still be constructed with ``RuntimeScope()``.
    A lifecycle root that needs parkable ownership uses :meth:`create_root`,
    and descendants are registered through :meth:`create_child`. Every child
    shares the root's :class:`SurvivorRegistry`, so reservations charge both
    the lifecycle-root quota and its runtime-wide supervisor quota.
    """

    def __init__(
        self,
        *,
        name: str = "runtime",
        parent: RuntimeScope | None = None,
        survivor_registry: SurvivorRegistry | None = None,
    ) -> None:
        if not name:
            raise ValueError("RuntimeScope name must be non-empty")
        if parent is not None and survivor_registry is not parent.survivor_registry:
            raise ValueError("Child RuntimeScope must share its parent's survivor registry")
        self._name = name
        self._parent = parent
        self._root = self if parent is None else parent.root
        self._survivor_registry = survivor_registry
        self._owner_id = name if parent is None else f"{parent.owner_id}/{name}"
        self._children: dict[str, RuntimeScope] = {}
        self._tasks: dict[str, set[asyncio.Task[Any]]] = {}

    @classmethod
    def create_root(
        cls,
        *,
        name: str,
        root_id: str,
        supervisor: RuntimeSupervisor,
        survivor_capacity: int,
    ) -> Self:
        """Create an explicitly attached lifecycle root."""
        registry = SurvivorRegistry(
            supervisor=supervisor,
            root_id=root_id,
            capacity=survivor_capacity,
        )
        return cls(name=name, survivor_registry=registry)

    @property
    def name(self) -> str:
        """Stable name within the parent scope."""
        return self._name

    @property
    def owner_id(self) -> str:
        """Stable hierarchy-qualified owner label used by the registry."""
        return self._owner_id

    @property
    def parent(self) -> RuntimeScope | None:
        """Parent scope, or ``None`` for a lifecycle root."""
        return self._parent

    @property
    def root(self) -> RuntimeScope:
        """Lifecycle root shared by this scope and all descendants."""
        return self._root

    @property
    def survivor_registry(self) -> SurvivorRegistry | None:
        """Root registry shared by attached descendants."""
        return self._survivor_registry

    def children(self) -> tuple[RuntimeScope, ...]:
        """Return directly registered child scopes in creation order."""
        return tuple(self._children.values())

    def create_child(self, name: str) -> RuntimeScope:
        """Create and register one named child under this lifecycle."""
        if self._survivor_registry is None:
            raise RuntimeError("Child scopes require an explicitly attached lifecycle root")
        if not name:
            raise ValueError("RuntimeScope child name must be non-empty")
        if name in self._children:
            raise RuntimeError(f"RuntimeScope child {name!r} already exists")
        child = RuntimeScope(
            name=name,
            parent=self,
            survivor_registry=self._survivor_registry.for_child(),
        )
        self._children[name] = child
        return child

    async def start_owned_task(
        self,
        name: str,
        factory: Callable[[], Coroutine[Any, Any, _T]],
        *,
        task_name: str | None = None,
    ) -> asyncio.Task[_T]:
        """Reserve capacity, start a task, and retain it in this scope."""
        if not name:
            raise ValueError("RuntimeScope task name must be non-empty")
        registry = self._survivor_registry
        if registry is None:
            raise RuntimeError("Owned tasks require an explicitly attached lifecycle root")
        label = task_name or name
        if not label:
            raise ValueError("RuntimeScope task name must be non-empty")
        try:
            owned = await start_owned(
                factory,
                registry=registry,
                owner_id=self._owner_id,
                task_name=label,
            )
        except BaseException:
            # ``start_owned`` may receive caller cancellation after creating
            # and parking the child but before returning its handle. Recover
            # that exact registry-owned task into this scope's drain cohort.
            self._adopt_registry_tasks(name, task_name=label)
            raise
        return self.add_task(name, owned.task)

    def create_task(
        self,
        name: str,
        coro: Coroutine[Any, Any, _T],
        *,
        task_name: str | None = None,
    ) -> asyncio.Task[_T]:
        """Create and track a named task."""
        self._validate_new_task_name(name, coro)
        try:
            task = asyncio.create_task(coro, name=task_name or name)
        except BaseException:
            coro.close()
            raise
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
        self._validate_new_task_name(name, coro)

        try:
            task = asyncio.create_task(coro, name=name)
        except BaseException:
            coro.close()
            raise

        # Resolve the turn id once at scheduling time so the terminal
        # record carries the same id even if a new turn has started by
        # the time the task completes.  The task cannot run before this
        # synchronous setup finishes, but creating it first avoids recording
        # a phantom scheduled task when no event loop is available.
        try:
            resolved_turn = journal_sink.current_turn_id(turn_id)
            journal_sink.append_record(
                name="task_scheduled",
                turn_id=resolved_turn,
                data={"task_name": name},
            )
        except BaseException:
            # The caller never receives the task when journaling setup fails.
            # Cancel and observe it here instead of leaving an unowned task
            # running after the synchronous failure.
            task.cancel()
            task.add_done_callback(self.log_task_exception)
            raise

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
        """Return tracked tasks in this scope and its descendants."""
        if name is not None:
            own = tuple(self._tasks.get(name, ()))
        else:
            own = tuple(task for tasks in self._tasks.values() for task in tasks)
        return (*own, *(task for child in self._children.values() for task in child.tasks(name)))

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
            # Deliver a cancellation that was already pending when drain()
            # was entered before sampling the stale-request baseline below.
            # A previously caught request leaves cancelling() non-zero but
            # does not raise at this checkpoint.
            await checkpoint_pending_cancellation(current)
            cancellation_requests = current.cancelling() if current is not None else 0
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if current is not None and current.cancelling() > cancellation_requests:
                    raise
                if not cancel and pending is None:
                    pending = exc
            except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
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
        for child in self._children.values():
            if task in child.tasks():
                child._discard_task(task)
                return

    def _adopt_registry_tasks(self, name: str, *, task_name: str) -> None:
        registry = self._survivor_registry
        if registry is None:
            return
        tracked = set(self.tasks(name))
        for owned in registry.owned_tasks(self._owner_id):
            if owned.task_name == task_name and owned.task not in tracked:
                self.add_task(name, owned.task)

    @staticmethod
    def _validate_new_task_name(name: str, coro: Coroutine[Any, Any, Any]) -> None:
        """Reject an invalid tracked-task name before scheduling *coro*."""
        if not name:
            coro.close()
            raise ValueError("RuntimeScope task name must be non-empty")
