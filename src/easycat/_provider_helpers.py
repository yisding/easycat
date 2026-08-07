"""Shared helpers for STT/TTS provider implementations."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from easycat.events import WordTimestamp
from easycat.runtime._event_tasks import RuntimeEventTaskScope
from easycat.runtime.scope import RuntimeScope

if TYPE_CHECKING:
    from easycat.events import ErrorStage

logger = logging.getLogger(__name__)

_PROVIDER_EVENT_TASK_NAME = "provider_error_emit"
_PROVIDER_EVENT_COHORT = "provider-events"


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
        label = self._provider_error_name or "unknown"
        self._event_tasks = RuntimeEventTaskScope(
            owner_label=f"{label}-provider",
            member_name=_PROVIDER_EVENT_TASK_NAME,
            cohort=_PROVIDER_EVENT_COHORT,
            logger=logger,
            failure_message="Provider Error event emission failed",
        )

    @property
    def _emit_scope(self) -> RuntimeScope | None:
        """Compatibility inspection of the provider event scope."""
        return self._event_tasks.scope

    @property
    def _owns_emit_root(self) -> bool:
        """Compatibility inspection of standalone provider ownership."""
        return self._event_tasks.owns_root

    @property
    def _emit_tasks(self) -> set[asyncio.Task[Any]]:
        """Compatibility inspection of provider-event tasks owned by the scope."""
        return set(self._event_tasks.tasks())

    def _attach_provider_event_scope(self, parent: RuntimeScope, *, name: str) -> None:
        """Attach provider-event work to its owning application lifecycle."""
        self._event_tasks.attach(parent, name=name)

    def _ensure_emit_scope(self) -> RuntimeScope:
        return self._event_tasks.ensure_scope()

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
        self._event_tasks.create_task(
            bus.emit(
                Error(
                    exception=exc,
                    stage=self._error_stage,
                    provider=self._provider_error_name,
                )
            ),
            task_name=f"{self._provider_error_name}:error-emit",
        )

    async def _drain_emit_tasks(self) -> None:
        """Await any in-flight fire-and-forget ``_emit_provider_error`` tasks.

        Keeps teardown from leaving emit tasks dangling into interpreter
        shutdown ("Task was destroyed but it is pending"). Late emits are
        already safe (the journal sink no-ops after Session finalization), so
        this is lifecycle tidiness, not correctness.
        """
        scope = self._event_tasks.scope
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
        await self._event_tasks.release_standalone_if_empty()

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
