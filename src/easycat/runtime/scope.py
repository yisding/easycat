"""Runtime-owned background task scope."""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from threading import Lock
from typing import Any, Protocol, Self, TypeVar, runtime_checkable

from easycat._concurrency import (
    HardTimeoutStatus,
    OwnedTask,
    RuntimeSupervisor,
    SurvivorRegistry,
    checkpoint_pending_cancellation,
    hard_timeout,
    start_owned,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class RuntimeMemberKind(StrEnum):
    """Kind of lifecycle member represented by a terminal result."""

    TASK = "task"
    FINALIZER = "finalizer"


class RuntimeResultStatus(StrEnum):
    """Exhaustive terminal states retained by runtime scopes."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    RAISED = "raised"


@dataclass(frozen=True, slots=True)
class RuntimeTerminalResult:
    """Retained terminal value or error for a named lifecycle member."""

    owner_id: str
    name: str
    kind: RuntimeMemberKind
    status: RuntimeResultStatus
    task_name: str
    value: Any | None = None
    error: BaseException | None = None

    def unwrap(self) -> Any:
        """Return the value or re-raise the retained terminal error."""
        if self.error is not None:
            raise self.error
        return self.value


def _terminal_result_from_task(
    task: asyncio.Task[Any],
    *,
    owner_id: str,
    name: str,
    kind: RuntimeMemberKind,
) -> RuntimeTerminalResult:
    try:
        value = task.result()
    except asyncio.CancelledError as exc:
        return RuntimeTerminalResult(
            owner_id=owner_id,
            name=name,
            kind=kind,
            status=RuntimeResultStatus.CANCELLED,
            task_name=task.get_name(),
            error=exc,
        )
    except BaseException as exc:  # noqa: BLE001 - the result is retained for caller policy
        return RuntimeTerminalResult(
            owner_id=owner_id,
            name=name,
            kind=kind,
            status=RuntimeResultStatus.RAISED,
            task_name=task.get_name(),
            error=exc,
        )
    return RuntimeTerminalResult(
        owner_id=owner_id,
        name=name,
        kind=kind,
        status=RuntimeResultStatus.COMPLETED,
        task_name=task.get_name(),
        value=value,
    )


class BackgroundTaskScope:
    """Own self-pruning, fire-and-forget tasks for synchronous components.

    Unlike :class:`RuntimeScope`, this scope has no async drain boundary. It
    retains each task until completion, consumes its terminal result, and
    removes it automatically. Named tasks can be replaced or cancelled while
    the owning component keeps a synchronous ``stop()`` contract.
    """

    def __init__(self, *, name: str = "background") -> None:
        if not name:
            raise ValueError("BackgroundTaskScope name must be non-empty")
        self._name = name
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._terminal_results: list[RuntimeTerminalResult] = []

    def create_task(
        self,
        name: str,
        coro: Coroutine[Any, Any, _T],
        *,
        replace: bool = False,
        retain_result: bool = False,
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
        task.add_done_callback(partial(self._on_done, name, retain_result))
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

    def terminal_results(self, name: str | None = None) -> tuple[RuntimeTerminalResult, ...]:
        """Return retained named results in settlement order."""
        return tuple(
            result for result in self._terminal_results if name is None or result.name == name
        )

    def pop_terminal_results(
        self,
        name: str | None = None,
    ) -> tuple[RuntimeTerminalResult, ...]:
        """Remove and return retained results, optionally for one member."""
        selected = self.terminal_results(name)
        if name is None:
            self._terminal_results.clear()
        else:
            self._terminal_results = [
                result for result in self._terminal_results if result.name != name
            ]
        return selected

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

    def _on_done(
        self,
        name: str,
        retain_result: bool,
        task: asyncio.Task[Any],
    ) -> None:
        if self._tasks.get(name) is task:
            self._tasks.pop(name, None)
        if retain_result:
            self._terminal_results.append(
                _terminal_result_from_task(
                    task,
                    owner_id=self._name,
                    name=name,
                    kind=RuntimeMemberKind.TASK,
                )
            )
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


class RuntimeScopeState(StrEnum):
    """Observable lifecycle state for a runtime scope."""

    OPEN = "open"
    CLOSING = "closing"
    CLOSED_WITH_SURVIVORS = "closed_with_survivors"
    CLOSED = "closed"


class RuntimeTaskAction(StrEnum):
    """Task action selected when a teardown cohort is signalled."""

    FINISH = "finish"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class RuntimeMemberPolicy:
    """One mode's orthogonal teardown policy for a runtime member.

    ``grace_deadline`` and ``hard_deadline`` are loop-time budgets in
    seconds measured from the cohort's synchronous signal barrier. At the
    grace deadline, unfinished ``finish`` work is cancelled. At the hard
    deadline, unfinished owned work is parked in its survivor registry.
    """

    cohort: str
    signal_token: bool
    task_action: RuntimeTaskAction
    grace_deadline: float | None = None
    hard_deadline: float | None = None

    def __post_init__(self) -> None:
        if not self.cohort:
            raise ValueError("Runtime member cohort must be non-empty")
        for label, value in (
            ("grace_deadline", self.grace_deadline),
            ("hard_deadline", self.hard_deadline),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{label} must be non-negative")
        if (
            self.grace_deadline is not None
            and self.hard_deadline is not None
            and self.grace_deadline > self.hard_deadline
        ):
            raise ValueError("grace_deadline cannot exceed hard_deadline")


@dataclass(frozen=True, slots=True)
class RuntimeTaskPolicy:
    """Graceful and force policies for one runtime task member."""

    graceful: RuntimeMemberPolicy
    force: RuntimeMemberPolicy

    def for_mode(self, *, force: bool) -> RuntimeMemberPolicy:
        """Select the policy for the requested close mode."""
        return self.force if force else self.graceful


DEFAULT_RUNTIME_TASK_POLICY = RuntimeTaskPolicy(
    graceful=RuntimeMemberPolicy(
        cohort="default",
        signal_token=False,
        task_action=RuntimeTaskAction.FINISH,
    ),
    force=RuntimeMemberPolicy(
        cohort="default",
        signal_token=False,
        task_action=RuntimeTaskAction.CANCEL,
    ),
)


@dataclass(slots=True)
class _RuntimeTaskMember:
    scope: RuntimeScope
    name: str
    task: asyncio.Task[Any]
    policy: RuntimeTaskPolicy
    token_signal: Callable[[], object] | None
    owned: OwnedTask[Any] | None
    retain_result: bool
    parked: bool = False


@dataclass(slots=True)
class _RuntimeFinalizerNode:
    scope: RuntimeScope
    name: str
    factory: Callable[[], Coroutine[Any, Any, Any]]
    task: asyncio.Task[Any] | None = None
    retained_task: asyncio.Task[Any] | None = None
    retained_result: RuntimeTerminalResult | None = None
    completed: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeCohortSignal:
    """Snapshot produced by a synchronous cohort signal barrier."""

    cohort: str
    force: bool
    tasks: tuple[asyncio.Task[Any], ...]
    _root: RuntimeScope
    _started_at: float
    _members: tuple[_RuntimeTaskMember, ...]
    _signal_error: BaseException | None = None

    def includes_action(self, action: RuntimeTaskAction) -> bool:
        """Whether any snapshotted member selected ``action`` for this mode."""
        return any(
            member.policy.for_mode(force=self.force).task_action is action
            for member in self._members
        )


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
        default_policy: RuntimeTaskPolicy = DEFAULT_RUNTIME_TASK_POLICY,
    ) -> None:
        if not name:
            raise ValueError("RuntimeScope name must be non-empty")
        if parent is not None and survivor_registry is not parent.survivor_registry:
            raise ValueError("Child RuntimeScope must share its parent's survivor registry")
        self._name = name
        self._parent = parent
        self._root = self if parent is None else parent.root
        self._survivor_registry = survivor_registry
        self._default_policy = default_policy
        self._owner_id = name if parent is None else f"{parent.owner_id}/{name}"
        self._children: dict[str, RuntimeScope] = {}
        self._tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._members: dict[asyncio.Task[Any], _RuntimeTaskMember] = {}
        self._finalizers: dict[str, _RuntimeFinalizerNode] = {}
        self._terminal_results: list[RuntimeTerminalResult] = []
        self._state = RuntimeScopeState.OPEN
        self._state_lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._close_task: asyncio.Task[RuntimeScopeState] | None = None
        self._close_force = False
        self._close_joiners: set[asyncio.Task[Any]] = set()

    @classmethod
    def create_root(
        cls,
        *,
        name: str,
        root_id: str,
        supervisor: RuntimeSupervisor,
        survivor_capacity: int,
        default_policy: RuntimeTaskPolicy = DEFAULT_RUNTIME_TASK_POLICY,
    ) -> Self:
        """Create an explicitly attached lifecycle root."""
        registry = SurvivorRegistry(
            supervisor=supervisor,
            root_id=root_id,
            capacity=survivor_capacity,
        )
        return cls(
            name=name,
            survivor_registry=registry,
            default_policy=default_policy,
        )

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

    @property
    def state(self) -> RuntimeScopeState:
        """Current admission and settlement state."""
        with self._state_lock:
            return self._state

    def children(self) -> tuple[RuntimeScope, ...]:
        """Return directly registered child scopes in creation order."""
        return tuple(self._children.values())

    def add_finalizer(
        self,
        name: str,
        factory: Callable[[], Coroutine[Any, Any, Any]],
    ) -> None:
        """Register one ordered async finalizer without invoking its factory."""
        if inspect.iscoroutine(factory):
            factory.close()
            raise TypeError("RuntimeScope finalizer requires a factory")
        if not callable(factory):
            raise TypeError("RuntimeScope finalizer factory must be callable")
        if not name:
            raise ValueError("RuntimeScope finalizer name must be non-empty")
        self._require_open()
        if self.root._finalizer_named(name) is not None:
            raise RuntimeError(f"RuntimeScope finalizer {name!r} already exists")
        if name in self.root._policy_cohort_names():
            raise RuntimeError(f"RuntimeScope finalizer {name!r} collides with a task cohort")
        self._finalizers[name] = _RuntimeFinalizerNode(
            scope=self,
            name=name,
            factory=factory,
        )

    def terminal_results(self, name: str | None = None) -> tuple[RuntimeTerminalResult, ...]:
        """Return retained task and finalizer results across this subtree."""
        return tuple(
            result
            for scope in self._scope_tree()
            for result in scope._terminal_results
            if name is None or result.name == name
        )

    def pop_terminal_results(
        self,
        name: str | None = None,
    ) -> tuple[RuntimeTerminalResult, ...]:
        """Remove and return retained results across this subtree."""
        selected = self.terminal_results(name)
        for scope in self._scope_tree():
            if name is None:
                scope._terminal_results.clear()
            else:
                scope._terminal_results = [
                    result for result in scope._terminal_results if result.name != name
                ]
        return selected

    def create_child(
        self,
        name: str,
        *,
        default_policy: RuntimeTaskPolicy | None = None,
    ) -> RuntimeScope:
        """Create and register one named child under this lifecycle."""
        if self._survivor_registry is None:
            raise RuntimeError("Child scopes require an explicitly attached lifecycle root")
        if not name:
            raise ValueError("RuntimeScope child name must be non-empty")
        with self._state_lock:
            self._require_open_locked()
            if name in self._children:
                raise RuntimeError(f"RuntimeScope child {name!r} already exists")
            child = RuntimeScope(
                name=name,
                parent=self,
                survivor_registry=self._survivor_registry.for_child(),
                default_policy=default_policy or self._default_policy,
            )
            self._children[name] = child
        return child

    async def start_owned_task(
        self,
        name: str,
        factory: Callable[[], Coroutine[Any, Any, _T]],
        *,
        task_name: str | None = None,
        policy: RuntimeTaskPolicy | None = None,
        token_signal: Callable[[], object] | None = None,
        retain_result: bool = False,
    ) -> asyncio.Task[_T]:
        """Reserve capacity, start a task, and retain it in this scope."""
        if not name:
            raise ValueError("RuntimeScope task name must be non-empty")
        self._bind_running_loop()
        selected_policy = policy or self._default_policy
        self._validate_policy_signal(selected_policy, token_signal)
        self._validate_policy_cohorts(selected_policy)
        self._require_open()
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
            self._adopt_registry_tasks(
                name,
                task_name=label,
                policy=selected_policy,
                token_signal=token_signal,
                retain_result=retain_result,
            )
            raise
        return self._track_task(
            name,
            owned.task,
            policy=selected_policy,
            token_signal=token_signal,
            owned=owned,
            retain_result=retain_result,
        )

    def create_task(
        self,
        name: str,
        coro: Coroutine[Any, Any, _T],
        *,
        task_name: str | None = None,
        policy: RuntimeTaskPolicy | None = None,
        token_signal: Callable[[], object] | None = None,
        retain_result: bool = False,
    ) -> asyncio.Task[_T]:
        """Create and track a named task."""
        self._validate_new_task_name(name, coro)
        selected_policy = policy or self._default_policy
        try:
            self._bind_running_loop()
            self._validate_policy_signal(selected_policy, token_signal)
            self._validate_policy_cohorts(selected_policy)
            self._validate_raw_task_policy(selected_policy)
            self._require_open()
            task = asyncio.create_task(coro, name=task_name or name)
        except BaseException:
            coro.close()
            raise
        return self._track_task(
            name,
            task,
            policy=selected_policy,
            token_signal=token_signal,
            retain_result=retain_result,
        )

    def spawn_from_sync(
        self,
        name: str,
        factory: Callable[[], Coroutine[Any, Any, _T]],
        *,
        task_name: str | None = None,
        policy: RuntimeTaskPolicy | None = None,
        token_signal: Callable[[], object] | None = None,
        retain_result: bool = False,
    ) -> concurrent.futures.Future[asyncio.Task[_T]]:
        """Schedule a factory safely from another thread.

        The factory runs only on the scope's bound event-loop thread and only
        if admission is still open when that callback wins the close race.
        """
        result: concurrent.futures.Future[asyncio.Task[_T]] = concurrent.futures.Future()
        if inspect.iscoroutine(factory):
            factory.close()
            result.set_exception(TypeError("spawn_from_sync requires a factory"))
            return result
        if not callable(factory):
            result.set_exception(TypeError("spawn_from_sync factory must be callable"))
            return result
        if not name:
            result.set_exception(ValueError("RuntimeScope task name must be non-empty"))
            return result
        selected_policy = policy or self._default_policy
        try:
            self._validate_policy_signal(selected_policy, token_signal)
            self._validate_policy_cohorts(selected_policy)
            self._validate_raw_task_policy(selected_policy)
            with self.root._state_lock:
                loop = self.root._loop
            with self._state_lock:
                self._require_open_locked()
            if loop is None:
                raise RuntimeError("RuntimeScope must bind an event loop before thread spawn")
            loop.call_soon_threadsafe(
                self._spawn_from_sync_on_loop,
                result,
                name,
                factory,
                task_name,
                selected_policy,
                token_signal,
                retain_result,
            )
        except Exception as exc:  # noqa: BLE001 - cross-thread Future carries failure
            result.set_exception(exc)
        return result

    def create_journaled_task(
        self,
        coro: Coroutine[Any, Any, _T],
        *,
        name: str,
        journal_sink: JournalSink,
        turn_id: str | None = None,
        policy: RuntimeTaskPolicy | None = None,
        token_signal: Callable[[], object] | None = None,
        retain_result: bool = False,
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
        selected_policy = policy or self._default_policy

        try:
            self._bind_running_loop()
            self._validate_policy_signal(selected_policy, token_signal)
            self._validate_policy_cohorts(selected_policy)
            self._validate_raw_task_policy(selected_policy)
            self._require_open()
            task = asyncio.create_task(coro, name=name)
        except BaseException:
            coro.close()
            if self.state is not RuntimeScopeState.OPEN:
                try:
                    self._journal_rejected_task(journal_sink, name=name, turn_id=turn_id)
                except Exception:
                    logger.exception("Failed to journal rejected runtime task %r", name)
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
        return self._track_task(
            name,
            task,
            policy=selected_policy,
            token_signal=token_signal,
            retain_result=retain_result,
        )

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

    def add_task(
        self,
        name: str,
        task: asyncio.Task[_T],
        *,
        policy: RuntimeTaskPolicy | None = None,
        token_signal: Callable[[], object] | None = None,
        retain_result: bool = False,
    ) -> asyncio.Task[_T]:
        """Track an existing task under *name*.

        Adding under a name purges previously-tracked tasks for that
        same name that have already completed, so reusing a name
        (e.g. per-segment commit tasks) does not accumulate dead
        entries between drains. Pending tasks are left in place —
        call :meth:`drain` to observe their results and clear them.
        """
        if not name:
            raise ValueError("RuntimeScope task name must be non-empty")
        self._bind_running_loop()
        selected_policy = policy or self._default_policy
        self._validate_policy_signal(selected_policy, token_signal)
        self._validate_policy_cohorts(selected_policy)
        self._validate_raw_task_policy(selected_policy)
        self._require_open()
        return self._track_task(
            name,
            task,
            policy=selected_policy,
            token_signal=token_signal,
            retain_result=retain_result,
        )

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

    def cohorts(self, *, force: bool) -> tuple[str, ...]:
        """Return selected cohort names in stable member-registration order."""
        names: list[str] = []
        for member in self._task_members():
            cohort = member.policy.for_mode(force=force).cohort
            if cohort not in names:
                names.append(cohort)
        return tuple(names)

    def signal_cohort(
        self,
        cohort: str,
        *,
        force: bool,
        _exclude_tasks: set[asyncio.Task[Any]] | None = None,
    ) -> RuntimeCohortSignal:
        """Synchronously signal every selected member before any is awaited."""
        if not cohort:
            raise ValueError("Runtime cohort name must be non-empty")
        loop = self._bind_running_loop()
        members = tuple(
            member
            for member in self._task_members()
            if member.policy.for_mode(force=force).cohort == cohort
        )
        if _exclude_tasks:
            for member in members:
                if member.task in _exclude_tasks:
                    member.scope._discard_task(member.task)
            members = tuple(member for member in members if member.task not in _exclude_tasks)

        first_error: BaseException | None = None
        for member in members:
            selected = member.policy.for_mode(force=force)
            if selected.signal_token:
                assert member.token_signal is not None
                try:
                    member.token_signal()
                except BaseException as exc:  # noqa: BLE001 - finish the broadcast barrier
                    if first_error is None:
                        first_error = exc
            if selected.task_action is RuntimeTaskAction.CANCEL and not member.task.done():
                member.task.cancel()

        return RuntimeCohortSignal(
            cohort=cohort,
            force=force,
            tasks=tuple(member.task for member in members),
            _root=self,
            _started_at=loop.time(),
            _members=members,
            _signal_error=first_error,
        )

    async def drain_cohort(
        self,
        cohort: str | RuntimeCohortSignal,
        *,
        force: bool = False,
    ) -> None:
        """Drain one cohort snapshot with its selected escalation policy.

        Passing the ticket returned by :meth:`signal_cohort` preserves a
        visible broadcast-before-await barrier. Passing a name is shorthand
        for signalling and immediately draining that cohort.
        """
        signal = self.signal_cohort(cohort, force=force) if isinstance(cohort, str) else cohort
        if signal._root is not self:
            raise ValueError("Runtime cohort signal belongs to a different scope")
        await self._drain_cohort_signal(signal)

    async def close(
        self,
        *,
        force: bool = False,
        phases: tuple[str, ...] | None = None,
        supersede_timeout: float | None = None,
    ) -> RuntimeScopeState:
        """Close admission and drain policy cohorts in explicit phase order.

        A force caller replaces an active graceful close. The superseded
        controller is cancelled and given ``supersede_timeout`` seconds to
        unwind before the force controller proceeds; ``None`` waits for its
        cancellation to settle without a deadline.
        """
        if supersede_timeout is not None and supersede_timeout < 0:
            raise ValueError("supersede_timeout must be non-negative")
        self._bind_running_loop()
        if self.state is RuntimeScopeState.CLOSED:
            return RuntimeScopeState.CLOSED
        current = asyncio.current_task()
        if current is None:  # pragma: no cover - close always runs in a task
            raise RuntimeError("RuntimeScope.close() requires a running task")
        self._close_joiners.add(current)
        self._close_admission_recursive()

        try:
            while True:
                if self.state is RuntimeScopeState.CLOSED:
                    return RuntimeScopeState.CLOSED
                active = self._select_close_controller(
                    force=force,
                    phases=phases,
                    supersede_timeout=supersede_timeout,
                )

                cancellation_requests = current.cancelling()
                try:
                    return await asyncio.shield(active)
                except asyncio.CancelledError as exc:
                    self._require_superseded_controller(
                        active,
                        current=current,
                        cancellation_requests=cancellation_requests,
                        error=exc,
                    )
                    # A force caller cancelled the controller this caller had
                    # joined. Re-read ownership and join its replacement.
                    continue
        finally:
            self._close_joiners.discard(current)

    def _require_superseded_controller(
        self,
        active: asyncio.Task[RuntimeScopeState],
        *,
        current: asyncio.Task[Any],
        cancellation_requests: int,
        error: asyncio.CancelledError,
    ) -> None:
        if current.cancelling() > cancellation_requests:
            raise error
        if active is self._close_task:
            # The active controller propagated a member cancellation. Only a
            # controller replaced by a force close is retried.
            raise error

    def _select_close_controller(
        self,
        *,
        force: bool,
        phases: tuple[str, ...] | None,
        supersede_timeout: float | None,
    ) -> asyncio.Task[RuntimeScopeState]:
        active = self._close_task
        if active is None or active.done():
            replacement = asyncio.create_task(
                self._run_close(
                    force=force,
                    phases=phases,
                    superseded=None,
                    supersede_timeout=supersede_timeout,
                ),
                name=f"{self.owner_id}:close:{'force' if force else 'graceful'}",
            )
            replacement.add_done_callback(self._observe_close_controller)
            self._close_task = replacement
            self._close_force = force
            return replacement
        if not force or self._close_force:
            return active

        replacement = asyncio.create_task(
            self._run_close(
                force=True,
                phases=phases,
                superseded=active,
                supersede_timeout=supersede_timeout,
            ),
            name=f"{self.owner_id}:close:force",
        )
        replacement.add_done_callback(self._observe_close_controller)
        self._close_task = replacement
        self._close_force = True
        active.cancel()
        return replacement

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

    async def drain(
        self,
        name: str | None = None,
        *,
        cancel: bool = False,
        suppress_errors: bool = False,
    ) -> None:
        """Wait for pending tasks to finish, optionally cancelling them first.

        Every snapshotted task is awaited and discarded even if one of
        them fails; the first observed exception (if any) is re-raised
        once the drain completes, so callers cannot silently leave
        sibling tasks pending. When *cancel* is True, expected
        cancellation/exception teardown is swallowed. ``suppress_errors``
        preserves task execution while letting an owning emitter keep its
        reviewed log-and-drop result policy. Caller cancellation always
        propagates.
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
                if not cancel and not suppress_errors and pending is None:
                    pending = exc
            except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
                if not cancel and not suppress_errors and pending is None:
                    pending = exc
            finally:
                if task.done():
                    self._retain_task_result(task)
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
                self._members.pop(task, None)
                return
        for child in self._children.values():
            if task in child.tasks():
                child._discard_task(task)
                return

    def _adopt_registry_tasks(
        self,
        name: str,
        *,
        task_name: str,
        policy: RuntimeTaskPolicy,
        token_signal: Callable[[], object] | None,
        retain_result: bool,
    ) -> None:
        registry = self._survivor_registry
        if registry is None:
            return
        tracked = set(self.tasks(name))
        for owned in registry.owned_tasks(self._owner_id):
            if owned.task_name == task_name and owned.task not in tracked:
                member = self._track_task(
                    name,
                    owned.task,
                    policy=policy,
                    token_signal=token_signal,
                    owned=owned,
                    retain_result=retain_result,
                    _allow_closed=True,
                )
                if owned.state.value == "parked":
                    tracked_member = self._members[member]
                    self._retain_parked_member(tracked_member)

    async def _run_close(
        self,
        *,
        force: bool,
        phases: tuple[str, ...] | None,
        superseded: asyncio.Task[RuntimeScopeState] | None,
        supersede_timeout: float | None,
    ) -> RuntimeScopeState:
        if superseded is not None:
            done, _pending = await asyncio.wait(
                {superseded},
                timeout=supersede_timeout,
            )
            if done:
                try:
                    superseded.result()
                except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
                    pass
            else:
                logger.warning(
                    "Graceful RuntimeScope.close() ignored cancellation for %s",
                    self.owner_id,
                )

        selected_cohorts = self.cohorts(force=force)
        selected_finalizers = self._pending_finalizer_names()
        selected = (*selected_cohorts, *selected_finalizers)
        phase_order = selected if phases is None else phases
        if len(set(phase_order)) != len(phase_order) or any(not phase for phase in phase_order):
            raise ValueError("Runtime close phases must be unique non-empty names")
        missing = tuple(cohort for cohort in selected if cohort not in phase_order)
        if missing:
            raise ValueError(f"Runtime close phases omit selected cohorts: {missing!r}")

        first_error: BaseException | None = None
        for phase in phase_order:
            finalizer = self._finalizer_named(phase)
            if finalizer is not None:
                await self._run_finalizer(finalizer)
                continue
            signal = self.signal_cohort(
                phase,
                force=force,
                _exclude_tasks=self._close_joiners,
            )
            phase_error = await self._drain_close_phase(signal)
            first_error = first_error or phase_error

        self._mark_terminal_recursive()
        if first_error is not None:
            raise first_error
        return self.state

    async def _drain_close_phase(
        self,
        signal: RuntimeCohortSignal,
    ) -> BaseException | None:
        try:
            await self.drain_cohort(signal)
        except asyncio.CancelledError as exc:
            controller = asyncio.current_task()
            if controller is not None and controller.cancelling():
                raise
            return exc
        except BaseException as exc:  # noqa: BLE001 - settle every close phase first
            return exc
        return None

    async def _run_finalizer(self, node: _RuntimeFinalizerNode) -> None:
        if node.completed:
            return
        task = node.task
        if task is None:
            task = asyncio.create_task(
                self._invoke_finalizer(node),
                name=f"{node.scope.owner_id}:finalizer:{node.name}",
            )
            node.task = task

        current = asyncio.current_task()
        cancellation_requests = current.cancelling() if current is not None else 0
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if current is not None and current.cancelling() > cancellation_requests:
                task.add_done_callback(partial(node.scope._on_detached_finalizer_done, node))
                raise
            # The finalizer itself ended as cancelled; retain and propagate it
            # as a terminal result rather than treating it as close takeover.
        except BaseException:  # noqa: BLE001, S110 - retained and re-raised below
            # Inspect and retain the settled task below so the result model,
            # not the await expression, chooses propagation.
            pass

        if not task.done():  # pragma: no cover - shield only returns at settlement
            return
        result = node.scope._retain_finalizer_result(node, task)
        if node.task is task:
            node.task = None
        result.unwrap()

    def _on_detached_finalizer_done(
        self,
        node: _RuntimeFinalizerNode,
        task: asyncio.Task[Any],
    ) -> None:
        result = self._retain_finalizer_result(node, task)
        if result.status is RuntimeResultStatus.COMPLETED and node.task is task:
            node.task = None

    def _retain_finalizer_result(
        self,
        node: _RuntimeFinalizerNode,
        task: asyncio.Task[Any],
    ) -> RuntimeTerminalResult:
        if node.retained_task is task:
            assert node.retained_result is not None
            return node.retained_result
        result = _terminal_result_from_task(
            task,
            owner_id=node.scope.owner_id,
            name=node.name,
            kind=RuntimeMemberKind.FINALIZER,
        )
        node.scope._terminal_results.append(result)
        node.retained_task = task
        node.retained_result = result
        if result.status is RuntimeResultStatus.COMPLETED:
            node.completed = True
        return result

    @staticmethod
    async def _invoke_finalizer(node: _RuntimeFinalizerNode) -> Any:
        coroutine = node.factory()
        if isinstance(coroutine, asyncio.Future) or not inspect.iscoroutine(coroutine):
            raise TypeError("RuntimeScope finalizer factory must return a coroutine")
        return await coroutine

    async def _drain_cohort_signal(self, signal: RuntimeCohortSignal) -> None:
        pending = {member.task: member for member in signal._members}
        escalated: set[asyncio.Task[Any]] = set()
        errors = [] if signal._signal_error is None else [signal._signal_error]
        current = asyncio.current_task()
        loop = asyncio.get_running_loop()

        while pending:
            errors.extend(
                self._settle_done_members(
                    pending,
                    force=signal.force,
                    escalated=escalated,
                )
            )
            if not pending:
                break

            now = loop.time()
            self._apply_grace_deadlines(
                pending,
                force=signal.force,
                started_at=signal._started_at,
                now=now,
                escalated=escalated,
            )
            errors.extend(
                await self._apply_hard_deadlines(
                    pending,
                    force=signal.force,
                    started_at=signal._started_at,
                    now=now,
                    escalated=escalated,
                )
            )
            if not pending:
                break

            await checkpoint_pending_cancellation(current)
            cancellation_requests = current.cancelling() if current is not None else 0
            try:
                await asyncio.wait(
                    set(pending),
                    timeout=self._next_cohort_timeout(
                        pending,
                        force=signal.force,
                        started_at=signal._started_at,
                        now=loop.time(),
                        escalated=escalated,
                    ),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                if current is None or current.cancelling() > cancellation_requests:
                    raise

        if errors:
            raise errors[0]

    def _settle_done_members(
        self,
        pending: dict[asyncio.Task[Any], _RuntimeTaskMember],
        *,
        force: bool,
        escalated: set[asyncio.Task[Any]],
    ) -> list[BaseException]:
        errors: list[BaseException] = []
        for task in tuple(pending):
            if not task.done():
                continue
            member = pending.pop(task)
            error = self._settle_cohort_member(
                member,
                force=force,
                escalated=task in escalated,
            )
            if error is not None:
                errors.append(error)
        return errors

    @staticmethod
    def _apply_grace_deadlines(
        pending: dict[asyncio.Task[Any], _RuntimeTaskMember],
        *,
        force: bool,
        started_at: float,
        now: float,
        escalated: set[asyncio.Task[Any]],
    ) -> None:
        for task, member in pending.items():
            selected = member.policy.for_mode(force=force)
            if selected.task_action is not RuntimeTaskAction.FINISH:
                continue
            grace_at = (
                None if selected.grace_deadline is None else started_at + selected.grace_deadline
            )
            if grace_at is not None and task not in escalated and now >= grace_at:
                task.cancel()
                escalated.add(task)

    async def _apply_hard_deadlines(
        self,
        pending: dict[asyncio.Task[Any], _RuntimeTaskMember],
        *,
        force: bool,
        started_at: float,
        now: float,
        escalated: set[asyncio.Task[Any]],
    ) -> list[BaseException]:
        errors: list[BaseException] = []
        for task, member in tuple(pending.items()):
            selected = member.policy.for_mode(force=force)
            if selected.hard_deadline is None:
                continue
            hard_at = started_at + selected.hard_deadline
            if now < hard_at:
                continue
            assert member.owned is not None
            escalated.add(task)
            outcome = await hard_timeout(member.owned, hard_at)
            pending.pop(task)
            if outcome.status is HardTimeoutStatus.TIMED_OUT_PARKED:
                self._retain_parked_member(member)
                continue
            if outcome.status is HardTimeoutStatus.PARK_REJECTED_LOCK_HELD:
                if outcome.error is not None:
                    errors.append(outcome.error)
                continue
            error = self._settle_cohort_member(member, force=force, escalated=True)
            if error is None:
                error = outcome.error
            if error is not None:
                errors.append(error)
        return errors

    @staticmethod
    def _next_cohort_timeout(
        pending: dict[asyncio.Task[Any], _RuntimeTaskMember],
        *,
        force: bool,
        started_at: float,
        now: float,
        escalated: set[asyncio.Task[Any]],
    ) -> float | None:
        deadlines: list[float] = []
        for task, member in pending.items():
            selected = member.policy.for_mode(force=force)
            if (
                selected.task_action is RuntimeTaskAction.FINISH
                and selected.grace_deadline is not None
                and task not in escalated
            ):
                deadlines.append(started_at + selected.grace_deadline)
            if selected.hard_deadline is not None:
                deadlines.append(started_at + selected.hard_deadline)
        return None if not deadlines else max(min(deadlines) - now, 0.0)

    def _settle_cohort_member(
        self,
        member: _RuntimeTaskMember,
        *,
        force: bool,
        escalated: bool,
    ) -> BaseException | None:
        task = member.task
        selected = member.policy.for_mode(force=force)
        # The action only controls error suppression after policy cancellation;
        # a naturally failing finish-member still propagates its result.
        suppress = escalated or selected.task_action is RuntimeTaskAction.CANCEL
        error: BaseException | None = None
        try:
            task.result()
        except asyncio.CancelledError as exc:
            if not suppress:
                error = exc
        except BaseException as exc:  # noqa: BLE001 - close caller selects precedence
            if not suppress:
                error = exc
        member.scope._retain_task_result(task, member=member)
        member.scope._discard_task(task)
        return error

    def _retain_parked_member(self, member: _RuntimeTaskMember) -> None:
        if member.parked:
            return
        member.parked = True
        member.task.add_done_callback(member.scope._on_parked_member_done)

    def _on_parked_member_done(self, task: asyncio.Task[Any]) -> None:
        self._retain_task_result(task)
        self._discard_task(task)
        self._refresh_terminal_state_upwards()

    def _observe_close_controller(self, task: asyncio.Task[RuntimeScopeState]) -> None:
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None and not self._close_joiners:
            logger.error(
                "Detached RuntimeScope.close() failed for %s",
                self.owner_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _close_admission_recursive(self) -> None:
        for scope in self._scope_tree():
            with scope._state_lock:
                if scope._state is not RuntimeScopeState.CLOSED:
                    scope._state = RuntimeScopeState.CLOSING
            if scope._survivor_registry is not None:
                scope._survivor_registry.close_owner(scope.owner_id)

    def _mark_terminal_recursive(self) -> None:
        for scope in reversed(self._scope_tree()):
            with scope._state_lock:
                has_work = bool(scope.tasks())
                scope._state = (
                    RuntimeScopeState.CLOSED_WITH_SURVIVORS
                    if has_work
                    else RuntimeScopeState.CLOSED
                )

    def _refresh_terminal_state_upwards(self) -> None:
        scope: RuntimeScope | None = self
        while scope is not None:
            with scope._state_lock:
                if scope._state not in (
                    RuntimeScopeState.CLOSED,
                    RuntimeScopeState.CLOSED_WITH_SURVIVORS,
                ):
                    break
                scope._state = (
                    RuntimeScopeState.CLOSED_WITH_SURVIVORS
                    if scope.tasks()
                    else RuntimeScopeState.CLOSED
                )
            scope = scope.parent

    def _scope_tree(self) -> tuple[RuntimeScope, ...]:
        descendants = (scope for child in self._children.values() for scope in child._scope_tree())
        return (self, *descendants)

    def _task_members(self) -> tuple[_RuntimeTaskMember, ...]:
        return tuple(member for scope in self._scope_tree() for member in scope._members.values())

    def _pending_finalizer_names(self) -> tuple[str, ...]:
        return tuple(
            node.name
            for scope in self._scope_tree()
            for node in scope._finalizers.values()
            if not node.completed
        )

    def _finalizer_named(self, name: str) -> _RuntimeFinalizerNode | None:
        for scope in self._scope_tree():
            node = scope._finalizers.get(name)
            if node is not None:
                return node
        return None

    def _policy_cohort_names(self) -> set[str]:
        return {
            cohort
            for member in self._task_members()
            for cohort in (
                member.policy.graceful.cohort,
                member.policy.force.cohort,
            )
        }

    def _retain_task_result(
        self,
        task: asyncio.Task[Any],
        *,
        member: _RuntimeTaskMember | None = None,
    ) -> None:
        selected = member or self.root._member_for_task(task)
        if selected is None or not selected.retain_result or not task.done():
            return
        selected.retain_result = False
        selected.scope._terminal_results.append(
            _terminal_result_from_task(
                task,
                owner_id=selected.scope.owner_id,
                name=selected.name,
                kind=RuntimeMemberKind.TASK,
            )
        )

    def _track_task(
        self,
        name: str,
        task: asyncio.Task[_T],
        *,
        policy: RuntimeTaskPolicy,
        token_signal: Callable[[], object] | None,
        owned: OwnedTask[_T] | None = None,
        retain_result: bool = False,
        _allow_closed: bool = False,
    ) -> asyncio.Task[_T]:
        if not _allow_closed:
            self._require_open()
        existing_member = self.root._member_for_task(task)
        if existing_member is not None:
            if existing_member.scope is self and existing_member.name == name:
                return task
            raise RuntimeError("A runtime task may belong to only one scope member")
        bucket = self._tasks.setdefault(name, set())
        for existing in tuple(bucket):
            if existing.done():
                self._retain_task_result(existing)
                self._discard_task(existing)
        bucket = self._tasks.setdefault(name, set())
        bucket.add(task)
        self._members[task] = _RuntimeTaskMember(
            scope=self,
            name=name,
            task=task,
            policy=policy,
            token_signal=token_signal,
            owned=owned,
            retain_result=retain_result,
        )
        return task

    def _member_for_task(self, task: asyncio.Task[Any]) -> _RuntimeTaskMember | None:
        for scope in self._scope_tree():
            member = scope._members.get(task)
            if member is not None:
                return member
        return None

    def _spawn_from_sync_on_loop(
        self,
        result: concurrent.futures.Future[asyncio.Task[_T]],
        name: str,
        factory: Callable[[], Coroutine[Any, Any, _T]],
        task_name: str | None,
        policy: RuntimeTaskPolicy,
        token_signal: Callable[[], object] | None,
        retain_result: bool,
    ) -> None:
        if result.cancelled():
            return
        try:
            self._require_open()
            coroutine = factory()
            if isinstance(coroutine, asyncio.Future) or not inspect.iscoroutine(coroutine):
                raise TypeError("spawn_from_sync factory must return a coroutine")
            task = self.create_task(
                name,
                coroutine,
                task_name=task_name,
                policy=policy,
                token_signal=token_signal,
                retain_result=retain_result,
            )
        except BaseException as exc:  # noqa: BLE001 - cross-thread Future carries failure
            try:
                result.set_exception(exc)
            except concurrent.futures.InvalidStateError:
                pass
        else:
            try:
                result.set_result(task)
            except concurrent.futures.InvalidStateError:
                # The submitter cancelled its handle after the task won
                # admission. The scope still owns and drains that task.
                pass

    def _bind_running_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        root = self.root
        with root._state_lock:
            if root._loop is None:
                root._loop = loop
            elif root._loop is not loop:
                raise RuntimeError("RuntimeScope cannot span event loops")
        return loop

    def _require_open(self) -> None:
        with self._state_lock:
            self._require_open_locked()

    def _require_open_locked(self) -> None:
        if self._state is not RuntimeScopeState.OPEN:
            raise RuntimeError(f"RuntimeScope {self.owner_id!r} is {self._state.value}")

    def _validate_policy_cohorts(self, policy: RuntimeTaskPolicy) -> None:
        finalizer_names = {name for scope in self.root._scope_tree() for name in scope._finalizers}
        for cohort in (policy.graceful.cohort, policy.force.cohort):
            if cohort in finalizer_names:
                raise RuntimeError(f"Runtime task cohort {cohort!r} collides with a finalizer")

    @staticmethod
    def _validate_policy_signal(
        policy: RuntimeTaskPolicy,
        token_signal: Callable[[], object] | None,
    ) -> None:
        if (policy.graceful.signal_token or policy.force.signal_token) and token_signal is None:
            raise ValueError("A token signal callback is required by the runtime task policy")

    @staticmethod
    def _validate_raw_task_policy(policy: RuntimeTaskPolicy) -> None:
        if policy.graceful.hard_deadline is not None or policy.force.hard_deadline is not None:
            raise ValueError("Hard-deadline runtime members must start as owned tasks")

    @staticmethod
    def _journal_rejected_task(
        journal_sink: JournalSink,
        *,
        name: str,
        turn_id: str | None,
    ) -> None:
        resolved_turn = journal_sink.current_turn_id(turn_id)
        journal_sink.append_record(
            name="task_rejected",
            turn_id=resolved_turn,
            data={"task_name": name, "reason": "scope_closed"},
        )

    @staticmethod
    def _validate_new_task_name(name: str, coro: Coroutine[Any, Any, Any]) -> None:
        """Reject an invalid tracked-task name before scheduling *coro*."""
        if not name:
            coro.close()
            raise ValueError("RuntimeScope task name must be non-empty")
