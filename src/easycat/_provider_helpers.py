"""Shared helpers for STT/TTS provider implementations."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING, Any

from easycat.events import WordTimestamp
from easycat.runtime.scope import (
    RuntimeMemberPolicy,
    RuntimeScope,
    RuntimeScopeState,
    RuntimeTaskAction,
    RuntimeTaskPolicy,
)

if TYPE_CHECKING:
    from easycat.events import ErrorStage

logger = logging.getLogger(__name__)

_PROVIDER_EVENT_TASK_NAME = "provider_error_emit"
_PROVIDER_EVENT_COHORT = "provider-events"
_PROVIDER_EVENT_POLICY = RuntimeTaskPolicy(
    graceful=RuntimeMemberPolicy(
        cohort=_PROVIDER_EVENT_COHORT,
        signal_token=False,
        task_action=RuntimeTaskAction.FINISH,
    ),
    force=RuntimeMemberPolicy(
        cohort=_PROVIDER_EVENT_COHORT,
        signal_token=False,
        task_action=RuntimeTaskAction.FINISH,
    ),
)


class ProviderErrorEmitter:
    """Mixin that posts journal-visible ``Error`` events for provider failures.

    The STT and TTS WebSocket bases (and the HTTP STT/TTS providers) all need
    the same subtle async lifecycle: resolve an event bus, attach context as
    notes on the exception, fire a ``bus.emit(Error(...))`` task, and keep a
    *strong* reference to that task so the event loop (which only holds a weak
    one) cannot garbage-collect it mid-emit. This mixin owns that one copy.

    Subclasses configure two things:

    - ``_error_stage`` — the :class:`~easycat.events.ErrorStage` the event is
      tagged with (``STT`` or ``TTS``).
    - ``_provider_error_name`` — the ``provider`` string attached to the event.

    and may override :meth:`_resolve_event_bus` to change where the bus comes
    from (the default reads ``self._config.event_bus``; the STT base reads a
    per-connection ``self._provider_event_bus`` instead).

    With no event bus wired this is a no-op (the surrounding stage still records
    a ``stage_error`` for any propagated exception).
    """

    # Subclasses set these. Defaults keep the mixin importable on its own.
    _error_stage: ErrorStage
    _provider_error_name: str = "unknown"

    def _init_emit_tasks(self) -> None:
        """Initialize the lazily attached provider-event scope.

        Session-owned providers attach this emitter beneath the Session root.
        A provider used standalone lazily creates and drains its own supervised
        lifecycle root instead.
        """
        self._emit_scope: RuntimeScope | None = None
        self._owns_emit_root = False

    @property
    def _emit_tasks(self) -> set[asyncio.Task[Any]]:
        """Compatibility inspection of provider-event tasks owned by the scope."""
        scope = self._emit_scope
        return set() if scope is None else set(scope.tasks(_PROVIDER_EVENT_TASK_NAME))

    def _attach_provider_event_scope(self, parent: RuntimeScope, *, name: str) -> None:
        """Attach provider-event work to its owning application lifecycle."""
        if not name:
            raise ValueError("Provider event RuntimeScope name must be non-empty")
        current = self._emit_scope
        if current is not None:
            if current.parent is parent:
                return
            if current.tasks():
                raise RuntimeError(
                    "Cannot reattach provider event work while emissions are active"
                )
        self._emit_scope = parent.create_child(
            name,
            default_policy=_PROVIDER_EVENT_POLICY,
        )
        self._owns_emit_root = False

    def _ensure_emit_scope(self) -> RuntimeScope:
        scope = self._emit_scope
        if scope is not None:
            return scope
        label = self._provider_error_name or "unknown"
        scope = RuntimeScope(
            name=f"{label}-provider-events",
            default_policy=_PROVIDER_EVENT_POLICY,
        )
        self._emit_scope = scope
        self._owns_emit_root = True
        return scope

    def _resolve_event_bus(self) -> Any | None:
        """Return the event bus to emit on, or ``None`` to skip emission.

        Default: the ``event_bus`` attribute of ``self._config``. The STT
        WebSocket base overrides this to use a per-connection bus reference.
        """
        return getattr(getattr(self, "_config", None), "event_bus", None)

    def _emit_provider_error(self, exc: BaseException, **context: Any) -> None:
        """Post a journal-visible ``Error`` event, with provider context.

        Context values are attached as notes on the exception so the existing
        ``Error`` event shape carries them without needing a new event type;
        ``None`` values are skipped.
        """
        bus = self._resolve_event_bus()
        if bus is None:
            return
        from easycat.events import Error, _add_exception_notes

        _add_exception_notes(exc, **context)
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # no running loop
            logger.debug("Could not emit provider error - no running loop", exc_info=True)
            return
        scope = self._ensure_emit_scope()
        try:
            task = scope.create_task(
                _PROVIDER_EVENT_TASK_NAME,
                bus.emit(
                    Error(
                        exception=exc,
                        stage=self._error_stage,
                        provider=self._provider_error_name,
                    )
                ),
                task_name=f"{self._provider_error_name}:error-emit",
            )
        except RuntimeError:
            if scope.state is RuntimeScopeState.OPEN:
                raise
            logger.debug("Could not emit provider error - runtime scope is closed")
            return
        # RuntimeScope is the strong owner while the emit is pending. Preserve
        # the prior self-pruning behavior once dispatch settles.
        task.add_done_callback(partial(self._on_emit_done, scope))

    @staticmethod
    def _on_emit_done(scope: RuntimeScope, task: asyncio.Task[Any]) -> None:
        scope.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.debug(
                "Provider Error event emission failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _drain_emit_tasks(self) -> None:
        """Await any in-flight fire-and-forget ``_emit_provider_error`` tasks.

        Keeps teardown from leaving emit tasks dangling into interpreter
        shutdown ("Task was destroyed but it is pending"). Late emits are
        already safe (the journal sink no-ops after Session finalization), so
        this is lifecycle tidiness, not correctness.
        """
        scope = self._emit_scope
        if scope is None:
            return
        current = asyncio.current_task()
        if current in scope.tasks(_PROVIDER_EVENT_TASK_NAME):
            # An Error subscriber may initiate provider/session teardown from
            # inside the tracked emit task. Do not await sibling emit tasks
            # here either: another subscriber can be joining this same
            # teardown, which would otherwise create a cross-task cycle.
            return
        await scope.drain(_PROVIDER_EVENT_TASK_NAME, suppress_errors=True)
        if self._owns_emit_root and scope.empty:
            await scope.close()
            if self._emit_scope is scope:
                self._emit_scope = None
                self._owns_emit_root = False

    async def _drain_provider_error_tasks(self) -> None:
        """Implement STTBase's explicit provider-error drain hook."""
        await self._drain_emit_tasks()


def get_package_version(pkg: str) -> str:
    try:
        from importlib.metadata import version

        return version(pkg)
    except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
        return "unknown"


def word_timestamps_from_words(words: Any) -> list[WordTimestamp] | None:
    if not isinstance(words, list):
        return None

    timestamps: list[WordTimestamp] = []
    for item in words:
        if not isinstance(item, dict):
            continue
        word = item.get("word")
        if not isinstance(word, str):
            word = item.get("text")
        start = item.get("start")
        end = item.get("end")
        if not isinstance(word, str) or start is None or end is None:
            continue
        try:
            start_float = float(start)
            end_float = float(end)
        except (TypeError, ValueError):
            continue
        timestamps.append(WordTimestamp(word=word, start=start_float, end=end_float))

    return timestamps or None
