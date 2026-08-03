"""Runtime-owned background task scope."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from functools import partial
from typing import Any, Protocol, TypeVar, runtime_checkable

from easycat._concurrency import (
    OwnedTask,
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
    """Track named runtime tasks and provide consistent cancellation/drain.

    ``RuntimeScope()`` remains a detached compatibility scope for isolated
    collaborators. Lifecycle-owned work must instead start at
    :meth:`create_root`; attached roots can register named children, and every
    child shares the root's bounded survivor registry.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._name: str | None = None
        self._parent: RuntimeScope | None = None
        self._root: RuntimeScope = self
        self._registry: SurvivorRegistry | None = None
        self._children: dict[str, RuntimeScope] = {}
        self._owned_tasks: dict[asyncio.Task[Any], OwnedTask[Any]] = {}
        self._owned_task_sequence = 0

    @classmethod
    def create_root(
        cls,
        name: str,
        *,
        supervisor: RuntimeSupervisor,
        survivor_capacity: int,
    ) -> RuntimeScope:
        """Create an explicitly attached lifecycle root.

        Application runtimes may pass one shared ``RuntimeSupervisor`` to
        multiple roots. The per-root registry preserves lifecycle isolation
        while the supervisor enforces the aggregate runtime bound.
        """
        cls._validate_scope_name(name)
        scope = cls()
        scope._name = name
        scope._registry = SurvivorRegistry(
            supervisor=supervisor,
            root_id=name,
            capacity=survivor_capacity,
        )
        return scope

    def create_child(self, name: str) -> RuntimeScope:
        """Register and return one named child under this attached scope."""
        self._validate_scope_name(name)
        if self._registry is None:
            raise RuntimeError(
                "RuntimeScope children require an attached root created with create_root()"
            )
        if name in self._children:
            raise RuntimeError(f"RuntimeScope child {name!r} is already registered")

        child = RuntimeScope()
        child._name = name
        child._parent = self
        child._root = self._root
        child._registry = self._registry.for_child()
        self._children[name] = child
        return child

    @property
    def name(self) -> str | None:
        """Return this scope's attached name, or ``None`` when detached."""
        return self._name

    @property
    def parent(self) -> RuntimeScope | None:
        """Return the registered parent; lifecycle roots have no parent."""
        return self._parent

    @property
    def root(self) -> RuntimeScope:
        """Return the lifecycle root shared by this scope tree."""
        return self._root

    @property
    def children(self) -> tuple[RuntimeScope, ...]:
        """Return directly registered child scopes in registration order."""
        return tuple(self._children.values())

    @property
    def survivor_registry(self) -> SurvivorRegistry | None:
        """Return the root registry shared by attached scopes."""
        return self._registry

    async def create_owned_task(
        self,
        name: str,
        factory: Callable[[], Coroutine[Any, Any, _T]],
        *,
        task_name: str | None = None,
    ) -> asyncio.Task[_T]:
        """Reserve bounded ownership, create, and track one parkable task.

        The unique owner id represents this task attempt rather than the
        reusable scope. Parking one cancellation-resistant attempt can close
        its registry owner without preventing a later attempt in the same
        cohort.
        """
        if not name:
            raise ValueError("RuntimeScope task name must be non-empty")
        registry = self._registry
        if registry is None:
            raise RuntimeError(
                "Owned RuntimeScope tasks require an attached root created with create_root()"
            )

        self._owned_task_sequence += 1
        owner_id = f"{self._scope_path()}:{name}:{self._owned_task_sequence}"
        try:
            owned = await start_owned(
                factory,
                registry=registry,
                owner_id=owner_id,
                task_name=task_name or name,
            )
        except BaseException:
            # ``start_owned`` may have created and parked a task before a
            # newly-pending caller cancellation is delivered. Attach that
            # retained task to the scope tree before preserving the failure.
            self._attach_retained_owned_tasks(owner_id, name)
            raise

        self._owned_tasks[owned.task] = owned
        return self.add_task(name, owned.task)

    def park(self, task: asyncio.Task[Any]) -> bool:
        """Park a still-running bounded task through the root supervisor."""
        owned = self._find_owned_task(task)
        if owned is None:
            raise ValueError("Task is not a bounded member of this RuntimeScope tree")
        return owned.park()

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
        completed = {existing for existing in bucket if existing.done()}
        bucket.difference_update(completed)
        for existing in completed:
            self._owned_tasks.pop(existing, None)
        bucket.add(task)
        return task

    def tasks(self, name: str | None = None) -> tuple[asyncio.Task[Any], ...]:
        """Return tracked tasks in this scope and all registered descendants."""
        scopes = self._scope_tree()
        if name is not None:
            return tuple(task for scope in scopes for task in scope._tasks.get(name, ()))
        return tuple(task for scope in scopes for tasks in scope._tasks.values() for task in tasks)

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
        for scope in self._scope_tree():
            for name, tasks in tuple(scope._tasks.items()):
                if task in tasks:
                    tasks.discard(task)
                    scope._owned_tasks.pop(task, None)
                    if not tasks:
                        scope._tasks.pop(name, None)
                    return

    def _scope_tree(self) -> tuple[RuntimeScope, ...]:
        scopes = [self]
        for child in self._children.values():
            scopes.extend(child._scope_tree())
        return tuple(scopes)

    def _scope_path(self) -> str:
        names: list[str] = []
        scope: RuntimeScope | None = self
        while scope is not None:
            if scope._name is not None:
                names.append(scope._name)
            scope = scope._parent
        return "/".join(reversed(names))

    def _find_owned_task(self, task: asyncio.Task[Any]) -> OwnedTask[Any] | None:
        for scope in self._scope_tree():
            owned = scope._owned_tasks.get(task)
            if owned is not None:
                return owned
        return None

    def _attach_retained_owned_tasks(self, owner_id: str, name: str) -> None:
        registry = self._registry
        if registry is None:
            return
        for owned in registry.owned_tasks(owner_id):
            if owned.task in self._owned_tasks:
                continue
            self._owned_tasks[owned.task] = owned
            self.add_task(name, owned.task)

    @staticmethod
    def _validate_scope_name(name: str) -> None:
        if not name:
            raise ValueError("RuntimeScope name must be non-empty")

    @staticmethod
    def _validate_new_task_name(name: str, coro: Coroutine[Any, Any, Any]) -> None:
        """Reject an invalid tracked-task name before scheduling *coro*."""
        if not name:
            coro.close()
            raise ValueError("RuntimeScope task name must be non-empty")
