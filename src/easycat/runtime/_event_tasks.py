"""Shared RuntimeScope ownership for best-effort event tasks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from functools import partial
from typing import Any

from easycat.runtime.scope import (
    RuntimeMemberPolicy,
    RuntimeScope,
    RuntimeScopeState,
    RuntimeTaskAction,
    RuntimeTaskPolicy,
)


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
        self._policy = RuntimeTaskPolicy(
            graceful=RuntimeMemberPolicy(
                cohort=cohort,
                signal_token=False,
                task_action=RuntimeTaskAction.FINISH,
            ),
            force=RuntimeMemberPolicy(
                cohort=cohort,
                signal_token=False,
                task_action=RuntimeTaskAction.FINISH,
            ),
        )
        self._scope: RuntimeScope | None = None
        self._owns_root = False

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
            if current.tasks():
                raise RuntimeError("Cannot reattach event work while emissions are active")
        self._scope = parent.create_child(name, default_policy=self._policy)
        self._owns_root = False

    def bind(self, scope: RuntimeScope) -> None:
        """Use an existing scope when a component shares its owner's child."""
        current = self._scope
        if current is scope:
            return
        if current is not None and current.tasks():
            raise RuntimeError("Cannot rebind event work while emissions are active")
        self._scope = scope
        self._owns_root = False

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
        task.add_done_callback(partial(self._on_done, scope))
        return task

    def adopt_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """Adopt an existing task into the same ownership and result policy."""
        scope = self.ensure_scope()
        scope.add_task(self._member_name, task, policy=self._policy)
        task.add_done_callback(partial(self._on_done, scope))
        return task

    def discard_task(self, task: asyncio.Task[Any]) -> None:
        """Release one task from this scope before transferring ownership."""
        scope = self._scope
        if scope is not None:
            scope.discard(task)

    async def release_standalone_if_empty(self) -> None:
        """Close and release an empty lazily created root."""
        scope = self._scope
        if not self._owns_root or scope is None or not scope.empty:
            return
        await scope.close()
        if self._scope is scope:
            self._scope = None
            self._owns_root = False

    def _on_done(self, scope: RuntimeScope, task: asyncio.Task[Any]) -> None:
        scope.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._logger.debug(
                self._failure_message,
                exc_info=(type(error), error, error.__traceback__),
            )


# Event-oriented call sites keep the descriptive name introduced with the
# helper; lifecycle workers use the generic name above.
RuntimeEventTaskScope = RuntimeTaskScope
