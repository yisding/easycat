"""Shared RuntimeScope ownership for best-effort event tasks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Coroutine
from typing import Any

from easycat.runtime.scope import (
    BackgroundTaskScope,
    RuntimeMemberPolicy,
    RuntimeScope,
    RuntimeScopeState,
    RuntimeTaskAction,
    RuntimeTaskPolicy,
)


async def wait_for_owned_future(
    future: asyncio.Future[Any],
    *,
    timeout_s: float,
) -> bool:
    """Apply a hard duration bound without creating or retaining hidden work.

    The caller must establish and retain ownership before calling this helper.
    A timeout requests cancellation, gives cooperative cleanup one event-loop
    turn, and returns ``False`` without waiting for cancellation-resistant work.
    Cancelling the caller leaves the owned future running and propagates the
    caller's cancellation.
    """
    if not isinstance(future, asyncio.Future):
        raise TypeError("wait_for_owned_future requires an already-owned Future or Task")
    done, _pending = await asyncio.wait({future}, timeout=max(timeout_s, 0.0))
    if future not in done:
        future.cancel()
        await asyncio.sleep(0)
        return False
    await future
    return True


class RuntimeTaskScope:
    """Own self-pruning tasks under an attached or standalone root."""

    def __init__(
        self,
        *,
        owner_label: str,
        member_name: str,
        cohort: str,
        logger: logging.Logger,
        failure_message: str,
        drop_if_closed: bool = True,
        release_standalone_when_idle: bool = False,
        graceful_action: RuntimeTaskAction = RuntimeTaskAction.FINISH,
        force_action: RuntimeTaskAction = RuntimeTaskAction.FINISH,
    ) -> None:
        if not owner_label:
            raise ValueError("Event task owner label must be non-empty")
        if not member_name:
            raise ValueError("Event task member name must be non-empty")
        self._owner_label = owner_label
        self._member_name = member_name
        self._logger = logger
        self._failure_message = failure_message
        self._drop_if_closed = drop_if_closed
        self._release_standalone_when_idle = release_standalone_when_idle
        self._policy = RuntimeTaskPolicy(
            graceful=RuntimeMemberPolicy(
                cohort=cohort,
                signal_token=False,
                task_action=graceful_action,
            ),
            force=RuntimeMemberPolicy(
                cohort=cohort,
                signal_token=False,
                task_action=force_action,
            ),
        )
        self._scope: RuntimeScope | None = None
        self._owns_root = False
        self._retired_roots: list[RuntimeScope] = []
        self._release_task_scope = BackgroundTaskScope(name=f"{owner_label}-idle-scope-releases")
        self._release_tasks: set[asyncio.Task[None]] = set()
        self._release_serial = 0

    @property
    def scope(self) -> RuntimeScope | None:
        """Return the currently attached or standalone scope."""
        return self._scope

    @property
    def owns_root(self) -> bool:
        """Whether this owner lazily created its current standalone root."""
        return self._owns_root

    @property
    def member_name(self) -> str:
        """Stable RuntimeScope member name shared by these event tasks."""
        return self._member_name

    def tasks(self) -> tuple[asyncio.Task[Any], ...]:
        """Return currently owned event tasks."""
        scope = self._scope
        return () if scope is None else scope.tasks(self._member_name)

    def attach(self, parent: RuntimeScope, *, name: str) -> None:
        """Attach event work beneath an application lifecycle root."""
        if not name:
            raise ValueError("Event task RuntimeScope name must be non-empty")
        current = self._scope
        if current is not None:
            if current.parent is parent:
                return
            if current.tasks() and not self._owns_root:
                raise RuntimeError("Cannot reattach event work while emissions are active")
        self._require_promotion_loop(current)
        self._replace_scope(parent.create_child(name, default_policy=self._policy))

    def bind(self, scope: RuntimeScope) -> None:
        """Use an existing scope when a component shares its owner's child."""
        current = self._scope
        if current is scope:
            return
        if current is not None and current.tasks() and not self._owns_root:
            raise RuntimeError("Cannot rebind event work while emissions are active")
        self._require_promotion_loop(current)
        self._replace_scope(scope)

    def ensure_scope(self) -> RuntimeScope:
        """Return the attached scope or lazily create a standalone root."""
        scope = self._scope
        if scope is not None:
            return scope
        scope = RuntimeScope(
            name=f"{self._owner_label}-events",
            default_policy=self._policy,
        )
        self._scope = scope
        self._owns_root = True
        return scope

    def create_task(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        task_name: str,
    ) -> asyncio.Task[Any] | None:
        """Create one strongly owned, self-pruning event task."""
        scope = self.ensure_scope()
        try:
            task = scope.create_task(
                self._member_name,
                coro,
                task_name=task_name,
                policy=self._policy,
            )
        except RuntimeError:
            if scope.state is RuntimeScopeState.OPEN or not self._drop_if_closed:
                raise
            coro.close()
            self._logger.debug("Could not start task - runtime scope is closed")
            return None
        task.add_done_callback(self._on_done)
        return task

    def create_awaitable_task(
        self,
        awaitable: Awaitable[Any],
        *,
        task_name: str,
    ) -> asyncio.Task[Any] | None:
        """Create one owned task from an SDK-specific non-coroutine awaitable."""
        scope = self.ensure_scope()
        try:
            task = scope.create_awaitable_task(
                self._member_name,
                awaitable,
                task_name=task_name,
                policy=self._policy,
            )
        except RuntimeError:
            if scope.state is RuntimeScopeState.OPEN or not self._drop_if_closed:
                raise
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            self._logger.debug("Could not start task - runtime scope is closed")
            return None
        task.add_done_callback(self._on_done)
        return task

    def adopt_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """Adopt an existing task into the same ownership and result policy."""
        scope = self.ensure_scope()
        scope.add_task(self._member_name, task, policy=self._policy)
        task.add_done_callback(self._on_done)
        return task

    def discard_task(self, task: asyncio.Task[Any]) -> None:
        """Release one task from this scope before transferring ownership."""
        scope = self._scope
        if scope is not None:
            scope.discard(task)

    async def cancel_and_drain(self) -> None:
        """Cancel and join this owner's tasks, closing an empty standalone root."""
        scope = self._scope
        if scope is None:
            return
        await scope.cancel_and_drain(self._member_name)
        await self.release_standalone_if_empty()

    async def release_standalone_if_empty(self) -> None:
        """Close and release an empty lazily created root."""
        for retired in tuple(self._retired_roots):
            if retired.empty:
                self._retired_roots.remove(retired)
                await retired.close()
        scope = self._scope
        if not self._owns_root or scope is None or not scope.empty:
            return
        # Detach before the first await. A new operation can then create a
        # fresh root while this empty one closes, and a loop-shutdown
        # cancellation cannot leave the owner pointing at the old loop.
        self._scope = None
        self._owns_root = False
        await scope.close()

    def _replace_scope(self, scope: RuntimeScope) -> None:
        """Install *scope*, promoting work from a standalone root if needed."""
        current = self._scope
        if current is None:
            self._scope = scope
            self._owns_root = False
            return

        tasks = current.tasks(self._member_name)
        added: list[asyncio.Task[Any]] = []
        try:
            for task in tasks:
                scope.add_task(self._member_name, task, policy=self._policy)
                added.append(task)
        except BaseException:
            for task in added:
                scope.discard(task)
            raise
        for task in tasks:
            current.discard(task)
        if self._owns_root:
            self._retired_roots.append(current)
        self._scope = scope
        self._owns_root = False

    def _require_promotion_loop(self, current: RuntimeScope | None) -> None:
        if current is None or not self._owns_root or not current.tasks(self._member_name):
            return
        loop = asyncio.get_running_loop()
        if any(task.get_loop() is not loop for task in current.tasks(self._member_name)):
            raise RuntimeError("Cannot promote runtime work across event loops")

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        scope = self._scope
        if scope is not None:
            scope.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self._logger.debug(
                    self._failure_message,
                    exc_info=(type(error), error, error.__traceback__),
                )
        if self._release_standalone_when_idle:
            self._schedule_standalone_release()

    def _schedule_standalone_release(self) -> None:
        """Release an idle standalone root on the loop that created it."""
        scope = self._scope
        if not self._owns_root or scope is None or not scope.empty:
            return
        self._release_serial += 1
        release = self._release_task_scope.create_task(
            f"{self._owner_label}-release-idle-scope-{self._release_serial}",
            self.release_standalone_if_empty(),
            log_errors=False,
        )
        self._release_tasks.add(release)
        release.add_done_callback(self._on_release_done)

    def _on_release_done(self, task: asyncio.Task[None]) -> None:
        self._release_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._logger.debug(
                "Could not release idle standalone runtime scope",
                exc_info=(type(error), error, error.__traceback__),
            )


# Event-oriented call sites keep the descriptive name introduced with the
# helper; lifecycle workers use the generic name above.
RuntimeEventTaskScope = RuntimeTaskScope
