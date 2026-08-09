"""Leaf concurrency primitives with explicit survivor ownership.

This module intentionally has no intra-package imports. It is the low-level
home for cancellation bookkeeping and for work that may outlive a bounded
teardown wait. Higher-level lifecycle scopes add policy; these types only
provide ownership, admission, and settlement mechanics.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Any, Generic, Self, TypeVar, cast

__all__ = [
    "CleanupSettlement",
    "HardTimeoutOutcome",
    "HardTimeoutStatus",
    "LifecycleLock",
    "LifecycleLockHeldError",
    "OwnedTask",
    "OwnedTaskMetadata",
    "OwnerState",
    "ReservationState",
    "RuntimeSupervisor",
    "SurvivorCapacityError",
    "SurvivorRegistry",
    "checkpoint_pending_cancellation",
    "hard_timeout",
    "reap",
    "shielded_cleanup",
    "start_owned",
    "swallow_cancel",
]

logger = logging.getLogger(__name__)

_T = TypeVar("_T")
JournalCallback = Callable[[str, Mapping[str, object]], None]


class ReservationState(StrEnum):
    """Exhaustive states for one root-and-runtime capacity reservation."""

    RESERVED = "reserved"
    ACTIVE = "active"
    PARKED = "parked"
    RELEASED = "released"


class OwnerState(StrEnum):
    """Observable lifecycle-owner state after admission closes."""

    OPEN = "open"
    CLOSED_WITH_SURVIVORS = "closed_with_survivors"
    CLOSED = "closed"


class HardTimeoutStatus(StrEnum):
    """Settlement selected by :func:`hard_timeout`."""

    COMPLETED = "completed"
    TIMED_OUT_PARKED = "timed_out_parked"
    PARK_REJECTED_LOCK_HELD = "park_rejected_lock_held"


class SurvivorCapacityError(RuntimeError):
    """Raised when root or runtime survivor admission is exhausted."""

    def __init__(self, quota: str, capacity: int) -> None:
        self.quota = quota
        self.capacity = capacity
        super().__init__(f"{quota} survivor capacity {capacity} is exhausted")


class LifecycleLockHeldError(RuntimeError):
    """Raised when parking would strand a lifecycle lock forever."""

    def __init__(self, owner_id: str, task_name: str) -> None:
        self.owner_id = owner_id
        self.task_name = task_name
        super().__init__(
            f"Cannot park task {task_name!r} for owner {owner_id!r} while it holds "
            "a lifecycle lock"
        )


@dataclass(frozen=True, slots=True)
class OwnedTaskMetadata:
    """Stable, owner-object-free metadata retained by a runtime supervisor."""

    reservation_id: int
    root_id: str
    owner_id: str
    task_name: str
    state: ReservationState


@dataclass(frozen=True, slots=True)
class CleanupSettlement(Generic[_T]):
    """Result of cleanup joined despite caller cancellation requests."""

    result: _T | None
    error: BaseException | None
    cancellation_requests: int

    @property
    def succeeded(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True)
class HardTimeoutOutcome:
    """Typed hard-timeout result; callers retain exception policy."""

    status: HardTimeoutStatus
    error: BaseException | None = None

    @property
    def completed(self) -> bool:
        return self.status is HardTimeoutStatus.COMPLETED


@dataclass(slots=True)
class _Reservation:
    reservation_id: int
    root_id: str
    owner_id: str
    task_name: str
    state: ReservationState = ReservationState.RESERVED
    task: asyncio.Task[Any] | None = None

    def metadata(self) -> OwnedTaskMetadata:
        return OwnedTaskMetadata(
            reservation_id=self.reservation_id,
            root_id=self.root_id,
            owner_id=self.owner_id,
            task_name=self.task_name,
            state=self.state,
        )


async def checkpoint_pending_cancellation(task: asyncio.Task[Any] | None = None) -> None:
    """Deliver a pending cancellation while ignoring a previously caught one."""
    if task is None:
        task = asyncio.current_task()
    if task is not None and task.cancelling():
        await asyncio.sleep(0)


class RuntimeSupervisor:
    """Runtime-wide strong anchor and aggregate survivor admission limit.

    Construct one supervisor for an application/event-loop runtime and pass it
    to every lifecycle-root registry. The supervisor retains tasks and stable
    string metadata, never lifecycle owner objects.
    """

    def __init__(
        self,
        *,
        capacity: int,
        journal: JournalCallback | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("RuntimeSupervisor capacity must be positive")
        self.capacity = capacity
        self._journal = journal
        self._reservations: dict[int, _Reservation] = {}
        self._next_reservation_id = 1
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lifecycle_lock_counts: dict[asyncio.Task[Any], int] = {}

    @property
    def active_count(self) -> int:
        return len(self._reservations)

    @property
    def survivor_count(self) -> int:
        return sum(
            reservation.state is ReservationState.PARKED
            for reservation in self._reservations.values()
        )

    def reservations(self) -> tuple[OwnedTaskMetadata, ...]:
        """Return stable metadata for all charged reservations."""
        return tuple(item.metadata() for item in self._reservations.values())

    def survivors(self) -> tuple[OwnedTaskMetadata, ...]:
        """Return stable metadata for parked survivors."""
        return tuple(
            item.metadata()
            for item in self._reservations.values()
            if item.state is ReservationState.PARKED
        )

    def tasks(self) -> tuple[asyncio.Task[Any], ...]:
        """Return the tasks strongly anchored by this runtime."""
        return tuple(
            reservation.task
            for reservation in self._reservations.values()
            if reservation.task is not None
        )

    def _reserve(self, *, root_id: str, owner_id: str, task_name: str) -> _Reservation:
        self._bind_running_loop()
        if self.active_count >= self.capacity:
            self._emit(
                "owned_task_rejected",
                root_id=root_id,
                owner_id=owner_id,
                task_name=task_name,
                quota="runtime",
            )
            raise SurvivorCapacityError("runtime", self.capacity)
        reservation = _Reservation(
            reservation_id=self._next_reservation_id,
            root_id=root_id,
            owner_id=owner_id,
            task_name=task_name,
        )
        self._next_reservation_id += 1
        self._reservations[reservation.reservation_id] = reservation
        self._emit_transition(reservation)
        return reservation

    def _activate(self, reservation: _Reservation, task: asyncio.Task[Any]) -> None:
        self._require_state(reservation, ReservationState.RESERVED)
        reservation.task = task
        reservation.state = ReservationState.ACTIVE
        self._emit_transition(reservation)

    def _park(self, reservation: _Reservation) -> None:
        self._require_state(reservation, ReservationState.ACTIVE)
        reservation.state = ReservationState.PARKED
        self._emit_transition(reservation)

    def _release(self, reservation: _Reservation, *, reason: str) -> None:
        if reservation.state is ReservationState.RELEASED:
            return
        reservation.state = ReservationState.RELEASED
        self._reservations.pop(reservation.reservation_id, None)
        self._emit_transition(reservation, reason=reason)

    def _task_holds_lifecycle_lock(self, task: asyncio.Task[Any]) -> bool:
        return self._lifecycle_lock_counts.get(task, 0) > 0

    def _lifecycle_lock_acquired(self, task: asyncio.Task[Any]) -> None:
        self._lifecycle_lock_counts[task] = self._lifecycle_lock_counts.get(task, 0) + 1

    def _lifecycle_lock_released(self, task: asyncio.Task[Any]) -> None:
        count = self._lifecycle_lock_counts.get(task, 0)
        if count <= 1:
            self._lifecycle_lock_counts.pop(task, None)
        else:
            self._lifecycle_lock_counts[task] = count - 1

    def _bind_running_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("RuntimeSupervisor cannot span event loops")

    def _emit_transition(self, reservation: _Reservation, *, reason: str | None = None) -> None:
        data: dict[str, object] = {
            "reservation_id": reservation.reservation_id,
            "root_id": reservation.root_id,
            "owner_id": reservation.owner_id,
            "task_name": reservation.task_name,
            "state": reservation.state.value,
        }
        if reason is not None:
            data["reason"] = reason
        self._emit("owned_task_transition", **data)

    def _emit(self, event: str, **data: object) -> None:
        if self._journal is None:
            return
        try:
            self._journal(event, data)
        except asyncio.CancelledError:
            logger.exception("Concurrency journal callback raised cancellation for %s", event)
        except Exception:
            logger.exception("Concurrency journal callback failed for %s", event)

    @staticmethod
    def _require_state(reservation: _Reservation, expected: ReservationState) -> None:
        if reservation.state is not expected:
            raise RuntimeError(
                f"Reservation {reservation.reservation_id} is {reservation.state}, "
                f"expected {expected}"
            )


class SurvivorRegistry:
    """Lifecycle-root quota sharing one runtime supervisor with other roots."""

    def __init__(
        self,
        *,
        supervisor: RuntimeSupervisor,
        root_id: str,
        capacity: int,
    ) -> None:
        if not root_id:
            raise ValueError("SurvivorRegistry root_id must be non-empty")
        if capacity < 1:
            raise ValueError("SurvivorRegistry capacity must be positive")
        self.supervisor = supervisor
        self.root_id = root_id
        self.capacity = capacity
        self._reservations: dict[int, _Reservation] = {}
        self._owned: dict[int, OwnedTask[Any]] = {}
        self._owner_states: dict[str, OwnerState] = {}

    @property
    def active_count(self) -> int:
        return len(self._reservations)

    @property
    def drained(self) -> bool:
        return not self._reservations

    def for_child(self) -> SurvivorRegistry:
        """Return the registry child lifecycle scopes must share."""
        return self

    def owner_state(self, owner_id: str) -> OwnerState:
        return self._owner_states.get(owner_id, OwnerState.OPEN)

    def close_owner(self, owner_id: str) -> OwnerState:
        """Close admission and report whether work remains for *owner_id*."""
        self._validate_label("owner_id", owner_id)
        has_work = any(item.owner_id == owner_id for item in self._reservations.values())
        state = OwnerState.CLOSED_WITH_SURVIVORS if has_work else OwnerState.CLOSED
        self._owner_states[owner_id] = state
        self.supervisor._emit(
            "owned_task_owner_closed",
            root_id=self.root_id,
            owner_id=owner_id,
            state=state.value,
        )
        return state

    def forget_closed_owner(self, owner_id: str) -> bool:
        """Drop settled owner metadata after its lifecycle scope is pruned."""
        self._validate_label("owner_id", owner_id)
        if self.owner_state(owner_id) is not OwnerState.CLOSED:
            return False
        if any(item.owner_id == owner_id for item in self._reservations.values()):
            return False
        self._owner_states.pop(owner_id, None)
        return True

    def reservations(self, owner_id: str | None = None) -> tuple[OwnedTaskMetadata, ...]:
        return tuple(
            item.metadata()
            for item in self._reservations.values()
            if owner_id is None or item.owner_id == owner_id
        )

    def survivors(self, owner_id: str | None = None) -> tuple[OwnedTaskMetadata, ...]:
        return tuple(
            item.metadata()
            for item in self._reservations.values()
            if item.state is ReservationState.PARKED
            and (owner_id is None or item.owner_id == owner_id)
        )

    def owned_tasks(self, owner_id: str | None = None) -> tuple[OwnedTask[Any], ...]:
        """Return retained handles so a closed owner may retry escalation."""
        return tuple(
            owned
            for owned in self._owned.values()
            if owner_id is None or owned.owner_id == owner_id
        )

    def _reserve(self, *, owner_id: str, task_name: str) -> _Reservation:
        self._validate_label("owner_id", owner_id)
        self._validate_label("task_name", task_name)
        if self.owner_state(owner_id) is not OwnerState.OPEN:
            raise RuntimeError(f"Lifecycle owner {owner_id!r} is closed")
        if self.active_count >= self.capacity:
            self.supervisor._emit(
                "owned_task_rejected",
                root_id=self.root_id,
                owner_id=owner_id,
                task_name=task_name,
                quota="root",
            )
            raise SurvivorCapacityError("root", self.capacity)
        reservation = self.supervisor._reserve(
            root_id=self.root_id,
            owner_id=owner_id,
            task_name=task_name,
        )
        self._reservations[reservation.reservation_id] = reservation
        return reservation

    def _activate(self, reservation: _Reservation, owned: OwnedTask[Any]) -> None:
        self._owned[reservation.reservation_id] = owned
        self.supervisor._activate(reservation, owned.task)

    def _park(self, reservation: _Reservation) -> None:
        self.supervisor._park(reservation)
        self._owner_states[reservation.owner_id] = OwnerState.CLOSED_WITH_SURVIVORS

    def _release(self, reservation: _Reservation, *, reason: str) -> None:
        if reservation.state is ReservationState.RELEASED:
            return
        owner_id = reservation.owner_id
        self._reservations.pop(reservation.reservation_id, None)
        self._owned.pop(reservation.reservation_id, None)
        self.supervisor._release(reservation, reason=reason)
        if self.owner_state(owner_id) is OwnerState.CLOSED_WITH_SURVIVORS and not any(
            item.owner_id == owner_id for item in self._reservations.values()
        ):
            self._owner_states[owner_id] = OwnerState.CLOSED
            self.supervisor._emit(
                "owned_task_owner_closed",
                root_id=self.root_id,
                owner_id=owner_id,
                state=OwnerState.CLOSED.value,
            )

    @staticmethod
    def _validate_label(label: str, value: str) -> None:
        if not value:
            raise ValueError(f"{label} must be non-empty")


class OwnedTask(Generic[_T]):
    """A task whose capacity reservation existed before it was spawned."""

    def __init__(
        self,
        task: asyncio.Task[_T],
        *,
        registry: SurvivorRegistry,
        reservation: _Reservation,
    ) -> None:
        self.task = task
        self.registry = registry
        self._reservation = reservation

    @property
    def state(self) -> ReservationState:
        return self._reservation.state

    @property
    def owner_id(self) -> str:
        return self._reservation.owner_id

    @property
    def task_name(self) -> str:
        return self._reservation.task_name

    @property
    def metadata(self) -> OwnedTaskMetadata:
        return self._reservation.metadata()

    def cancel(self) -> bool:
        return self.task.cancel()

    def park(self) -> bool:
        """Park once, retaining both quotas until eventual settlement."""
        if self.task.done():
            self._settle()
            return False
        if self.state in (ReservationState.PARKED, ReservationState.RELEASED):
            return False
        if self.state is not ReservationState.ACTIVE:
            raise RuntimeError(f"Cannot park reservation in state {self.state}")
        if self.registry.supervisor._task_holds_lifecycle_lock(self.task):
            raise LifecycleLockHeldError(self.owner_id, self.task_name)
        self.registry._park(self._reservation)
        return True

    def _on_done(self, _task: asyncio.Task[Any]) -> None:
        self._settle()

    def _settle(self) -> None:
        if self.state is ReservationState.RELEASED or not self.task.done():
            return
        _task_exception(self.task)
        prior_state = self.state
        self.registry._release(self._reservation, reason=f"{prior_state.value}_settled")


class LifecycleLock:
    """An asyncio lock that records its owning task with a runtime supervisor."""

    def __init__(self, supervisor: RuntimeSupervisor) -> None:
        self._supervisor = supervisor
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None

    async def acquire(self) -> bool:
        self._supervisor._bind_running_loop()
        await self._lock.acquire()
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - acquire() always runs in a task
            self._lock.release()
            raise RuntimeError("LifecycleLock requires a running task")
        self._owner = task
        self._supervisor._lifecycle_lock_acquired(task)
        return True

    def release(self) -> None:
        task = asyncio.current_task()
        if self._owner is not task:
            raise RuntimeError("LifecycleLock can only be released by its owning task")
        assert task is not None
        self._owner = None
        self._supervisor._lifecycle_lock_released(task)
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self) -> Self:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


async def start_owned(
    factory: Callable[[], Coroutine[Any, Any, _T]],
    *,
    registry: SurvivorRegistry,
    owner_id: str,
    task_name: str,
) -> OwnedTask[_T]:
    """Reserve both quotas, then invoke and schedule a parkable task factory."""
    if inspect.iscoroutine(factory):
        cast(Coroutine[Any, Any, Any], factory).close()
        raise TypeError("start_owned requires a factory, not a bare coroutine")
    if not callable(factory):
        raise TypeError("start_owned factory must be callable")

    current = asyncio.current_task()
    await checkpoint_pending_cancellation(current)
    reservation = registry._reserve(owner_id=owner_id, task_name=task_name)
    try:
        # The injected journal hook may synchronously request caller
        # cancellation while recording ``reserved``. Deliver that request
        # before the factory can acquire resources.
        await checkpoint_pending_cancellation(current)
        coroutine = factory()
        if isinstance(coroutine, asyncio.Future):
            raise TypeError("start_owned cannot adopt an already-running task or future")
        if not inspect.iscoroutine(coroutine):
            raise TypeError("start_owned factory must return a coroutine")
        try:
            task = asyncio.create_task(coroutine, name=task_name)
        except BaseException:
            coroutine.close()
            raise
    except BaseException:
        registry._release(reservation, reason="factory_failed")
        raise

    owned = OwnedTask(task, registry=registry, reservation=reservation)
    registry._activate(reservation, owned)
    task.add_done_callback(owned._on_done)
    try:
        # A factory may synchronously request caller cancellation while still
        # returning a coroutine. Never hand that newly-created task to a
        # cancelled caller without first retaining it as an owned survivor.
        await checkpoint_pending_cancellation(current)
    except asyncio.CancelledError:
        task.cancel()
        _park_or_close(owned)
        raise
    return owned


async def reap(owned: OwnedTask[Any], *, timeout: float | None = None) -> BaseException | None:
    """Cancel and await a child while preserving the caller's cancellation."""
    current = await _checkpoint_or_park_on_cancel(owned)
    cancellation_requests = current.cancelling() if current is not None else 0
    child_error: BaseException | None = None

    if not owned.task.done():
        owned.task.cancel()
    try:
        if timeout is None:
            await asyncio.shield(owned.task)
        else:
            done, _pending = await asyncio.wait({owned.task}, timeout=max(timeout, 0.0))
            if owned.task not in done:
                park_error = _park_or_close(owned)
                child_error = park_error or TimeoutError(
                    f"Timed out reaping task {owned.task_name!r}"
                )
    except asyncio.CancelledError as exc:
        if current is not None and current.cancelling() > cancellation_requests:
            _park_or_close(owned)
            raise
        child_error = exc
    except BaseException as exc:  # noqa: BLE001 - caller chooses child-exception policy
        child_error = exc

    if owned.task.done():
        owned._settle()
        if child_error is None:
            child_error = _task_exception(owned.task)

    await _checkpoint_or_park_on_cancel(owned, current=current)
    return child_error


async def hard_timeout(owned: OwnedTask[Any], deadline: float) -> HardTimeoutOutcome:
    """Wait to an absolute loop deadline, then cancel and park unfinished work."""
    current = await _checkpoint_or_park_on_cancel(owned)
    cancellation_requests = current.cancelling() if current is not None else 0
    loop = asyncio.get_running_loop()

    try:
        if not owned.task.done():
            remaining = max(deadline - loop.time(), 0.0)
            done, _pending = await asyncio.wait({owned.task}, timeout=remaining)
            if owned.task not in done:
                owned.task.cancel()
                park_error = _park_or_close(owned)
                if park_error is not None:
                    return HardTimeoutOutcome(
                        HardTimeoutStatus.PARK_REJECTED_LOCK_HELD,
                        park_error,
                    )
                return HardTimeoutOutcome(HardTimeoutStatus.TIMED_OUT_PARKED)
    except asyncio.CancelledError:
        if current is not None and current.cancelling() > cancellation_requests:
            if not owned.task.done():
                owned.task.cancel()
            _park_or_close(owned)
        raise

    owned._settle()
    error = _task_exception(owned.task)
    await _checkpoint_or_park_on_cancel(owned, current=current)
    return HardTimeoutOutcome(HardTimeoutStatus.COMPLETED, error)


async def shielded_cleanup(  # noqa: C901 - cancellation loop is intentionally explicit
    factory: Callable[[], Coroutine[Any, Any, _T]],
) -> CleanupSettlement[_T]:
    """Join cleanup to settlement while recording caller cancellation requests."""
    if inspect.iscoroutine(factory):
        cast(Coroutine[Any, Any, Any], factory).close()
        return CleanupSettlement(
            result=None,
            error=TypeError("shielded_cleanup requires a factory, not a bare coroutine"),
            cancellation_requests=0,
        )
    if not callable(factory):
        return CleanupSettlement(
            result=None,
            error=TypeError("shielded_cleanup factory must be callable"),
            cancellation_requests=0,
        )
    try:
        coroutine = factory()
        if not inspect.iscoroutine(coroutine):
            raise TypeError("shielded_cleanup factory must return a coroutine")
        try:
            task = asyncio.create_task(coroutine)
        except BaseException:
            coroutine.close()
            raise
    except BaseException as exc:  # noqa: BLE001 - settlement carries cancellation too
        return CleanupSettlement(result=None, error=exc, cancellation_requests=0)

    current = asyncio.current_task()
    cancellation_requests = 0
    last_seen = current.cancelling() if current is not None else 0
    try:
        await checkpoint_pending_cancellation(current)
    except asyncio.CancelledError:
        cancellation_requests += 1
        last_seen = current.cancelling() if current is not None else last_seen

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            count = current.cancelling() if current is not None else last_seen
            if count <= last_seen:
                break
            cancellation_requests += count - last_seen
            last_seen = count
        except BaseException:  # noqa: BLE001 - inspect the task after the join loop
            break

    try:
        await checkpoint_pending_cancellation(current)
    except asyncio.CancelledError:
        count = current.cancelling() if current is not None else last_seen + 1
        cancellation_requests += max(count - last_seen, 1)

    error = _task_exception(task)
    if error is not None:
        return CleanupSettlement(
            result=None,
            error=error,
            cancellation_requests=cancellation_requests,
        )
    return CleanupSettlement(
        result=task.result(),
        error=None,
        cancellation_requests=cancellation_requests,
    )


class _SwallowCancel:
    def __init__(self, journal: JournalCallback | None) -> None:
        self._journal = journal
        self._task: asyncio.Task[Any] | None = None
        self._baseline = 0

    async def __aenter__(self) -> None:
        self._task = asyncio.current_task()
        await checkpoint_pending_cancellation(self._task)
        self._baseline = self._task.cancelling() if self._task is not None else 0

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if not isinstance(exc, asyncio.CancelledError):
            return False
        current_count = self._task.cancelling() if self._task is not None else 0
        if self._task is not None and current_count > self._baseline:
            return False
        if self._journal is not None:
            try:
                self._journal(
                    "child_cancellation_swallowed",
                    {"cancellation_requests": current_count},
                )
            except asyncio.CancelledError:
                logger.exception("Concurrency journal callback raised cancellation")
            except Exception:
                logger.exception("Concurrency journal callback failed while swallowing cancel")
        return True


def swallow_cancel(*, journal: JournalCallback | None = None) -> _SwallowCancel:
    """Return the sanctioned async context manager for child-cancel suppression."""
    return _SwallowCancel(journal)


async def _checkpoint_or_park_on_cancel(
    owned: OwnedTask[Any],
    *,
    current: asyncio.Task[Any] | None = None,
) -> asyncio.Task[Any] | None:
    if current is None:
        current = asyncio.current_task()
    try:
        await checkpoint_pending_cancellation(current)
    except asyncio.CancelledError:
        if not owned.task.done():
            owned.task.cancel()
        _park_or_close(owned)
        raise
    return current


def _park_or_close(owned: OwnedTask[Any]) -> LifecycleLockHeldError | None:
    if owned.task.done():
        owned._settle()
        return None
    try:
        owned.park()
    except LifecycleLockHeldError as exc:
        owned.registry.close_owner(owned.owner_id)
        owned.registry.supervisor._emit(
            "owned_task_park_rejected",
            root_id=owned.registry.root_id,
            owner_id=owned.owner_id,
            task_name=owned.task_name,
            reason="lifecycle_lock_held",
        )
        return exc
    return None


def _task_exception(task: asyncio.Task[Any]) -> BaseException | None:
    if not task.done():
        return None
    if task.cancelled():
        return asyncio.CancelledError()
    try:
        return task.exception()
    except asyncio.CancelledError as exc:  # pragma: no cover - guarded by cancelled()
        return exc
