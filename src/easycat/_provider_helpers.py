"""Shared helpers for STT/TTS provider implementations."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from easycat.events import WordTimestamp

if TYPE_CHECKING:
    from easycat.events import ErrorStage

logger = logging.getLogger(__name__)


class ProviderErrorEmitter:
    """Mixin that posts journal-visible ``Error`` events for provider failures.

    The STT and TTS WebSocket bases (and OpenAI's HTTP TTS provider) all need
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
        """Initialize the fire-and-forget emit-task set.

        Called from the subclass ``__init__`` so the strong-reference set
        exists before the first :meth:`_emit_provider_error`.
        """
        # Strong references to fire-and-forget Error-emit tasks so the event
        # loop does not garbage-collect them before ``bus.emit`` completes.
        self._emit_tasks: set[asyncio.Task[Any]] = set()

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
        from easycat.events import Error

        for key, value in context.items():
            if value is None:
                continue
            try:
                exc.add_note(f"{key}={value}")  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - pre-3.11
                pass
        try:
            task = asyncio.create_task(
                bus.emit(
                    Error(
                        exception=exc,
                        stage=self._error_stage,
                        provider=self._provider_error_name,
                    )
                )
            )
        except RuntimeError:  # no running loop
            logger.debug("Could not emit provider error - no running loop", exc_info=True)
            return
        # Keep a strong reference until the emit completes; the event loop
        # only holds a weak one, so an untracked task can be GC'd mid-flight.
        self._emit_tasks.add(task)
        task.add_done_callback(self._emit_tasks.discard)

    async def _drain_emit_tasks(self) -> None:
        """Await any in-flight fire-and-forget ``_emit_provider_error`` tasks.

        Keeps teardown from leaving emit tasks dangling into interpreter
        shutdown ("Task was destroyed but it is pending"). Late emits are
        already safe (the journal sink no-ops after ``Session._destroy``), so
        this is lifecycle tidiness, not correctness.
        """
        if not self._emit_tasks:
            return
        # Snapshot: the done-callback mutates ``_emit_tasks`` during gather.
        await asyncio.gather(*list(self._emit_tasks), return_exceptions=True)


def get_package_version(pkg: str) -> str:
    try:
        from importlib.metadata import version

        return version(pkg)
    except Exception:
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
        timestamps.append(WordTimestamp(word=word, start=float(start), end=float(end)))

    return timestamps or None
