"""Thread-safe staleness fences with atomically captured payloads.

An :class:`Epoch` has one logical writer for its domain. ``bump(value)``
publishes a replacement payload and invalidates every previously captured
:class:`Lease`. A lease's ``value`` is the payload that was current in the
same mutex-protected operation that captured its generation; callers never
need to re-read a live pointer beside a staleness check.

``Lease.is_current()`` and ``Lease.guard()`` are exact only at the instant of
their check. Nothing remains atomic across an ``await``: scope cancellation
may request prompt unwinding, but a liveness-sensitive effect must re-guard
immediately before it commits. For example::

    lease = epoch.capture()
    if not lease.guard():
        return
    result = await prepare(lease.value)
    if lease.guard():
        commit(result)

The memory model is a mutex over generation, payload, bump, and capture.
``bump()`` may run from another thread. ``guard()`` is loop-only so off-loop
provider work must capture what it needs and arrange a final guard on the
event-loop thread. ``is_current()`` is thread-safe, but an off-loop result is
necessarily advisory because the epoch may bump before the caller acts.

This package-root module is intentionally a leaf with no intra-package
imports. It supplies mechanism only; domain owners define publication and
clear semantics, including bumping when a payload is cleared.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar, overload

__all__ = ["Epoch", "Lease", "Stale"]

_T = TypeVar("_T")
_StalePolicy = Literal["skip", "raise"]


class Stale(RuntimeError):
    """Raised when a lease guard requires freshness but observes staleness."""

    def __init__(self, lease_generation: int, current_generation: int) -> None:
        self.lease_generation = lease_generation
        self.current_generation = current_generation
        super().__init__(
            "Lease generation "
            f"{lease_generation} is stale; current generation is {current_generation}"
        )


class Epoch(Generic[_T]):
    """One thread-safe generation and its atomically published payload."""

    __slots__ = ("_generation", "_lock", "_value")

    def __init__(self, value: _T) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._value = value

    @property
    def generation(self) -> int:
        """Return the current generation as a thread-safe point-in-time read."""
        with self._lock:
            return self._generation

    def capture(self) -> Lease[_T]:
        """Capture the current generation and payload in one critical section."""
        with self._lock:
            return Lease(
                _epoch=self,
                generation=self._generation,
                value=self._value,
            )

    def bump(self, value: _T) -> int:
        """Publish ``value``, invalidate outstanding leases, and return the generation."""
        with self._lock:
            self._generation += 1
            self._value = value
            return self._generation

    def _current_generation(self) -> int:
        with self._lock:
            return self._generation


@dataclass(frozen=True, slots=True)
class Lease(Generic[_T]):
    """A generation check paired with the payload captured at that generation."""

    _epoch: Epoch[_T]
    generation: int
    value: _T

    def is_current(self) -> bool:
        """Check freshness at this instant; cross-thread callers must treat it as advisory."""
        return self.generation == self._epoch._current_generation()

    @overload
    def guard(self, *, on_stale: Literal["raise"]) -> Literal[True]: ...

    @overload
    def guard(self, *, on_stale: Literal["skip"] = "skip") -> bool: ...

    def guard(self, *, on_stale: _StalePolicy = "skip") -> bool:
        """Check freshness on an event loop, returning false or raising on staleness."""
        if on_stale not in ("skip", "raise"):
            raise ValueError("on_stale must be 'skip' or 'raise'")
        try:
            asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError("Lease.guard() requires a running event loop") from exc

        current_generation = self._epoch._current_generation()
        if self.generation == current_generation:
            return True
        if on_stale == "raise":
            raise Stale(self.generation, current_generation)
        return False
