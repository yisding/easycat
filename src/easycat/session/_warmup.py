"""Session startup warmup execution."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from easycat._audio_utils import resample_backend
from easycat.runtime.capabilities import warmup_if_supported, warmupable
from easycat.runtime.records import JournalRecordKind


class JournalSink(Protocol):
    """Journal surface needed for warmup records."""

    def append_record(
        self,
        *,
        name: str,
        kind: JournalRecordKind = JournalRecordKind.EVENT,
        turn_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> int | None: ...


WarmupComponent = tuple[str, Any]


class AudioResamplingWarmup:
    """Resolve optional numeric audio dependencies off the live audio path."""

    async def warmup(self) -> None:
        await asyncio.to_thread(resample_backend)


@dataclass(frozen=True, slots=True)
class WarmupRunner:
    """Run structural provider warmup hooks before user traffic starts."""

    enabled: bool
    journal_sink: JournalSink
    components: Sequence[WarmupComponent]

    async def run(self, *, select: Callable[[str], bool] | None = None) -> None:
        """Execute supported ``warmup()`` hooks and record startup timing.

        ``select`` filters which components run by name; when ``None`` every
        component runs.  The session uses it to warm providers/models before
        the transport is connected and warm the transport afterwards, so a
        transport ``warmup()`` that primes connect-initialized resources still
        runs after ``connect()``.  A filtered phase that warms nothing skips
        the ``warmup_completed`` record so transports without a warmup hook do
        not emit an empty second record on every startup.
        """
        if not self.enabled:
            return

        started = time.perf_counter()
        components = [
            (name, provider)
            for name, provider in self.components
            if (select is None or select(name)) and warmupable(provider) is not None
        ]
        if not components:
            if select is not None:
                return
            # Preserve the empty-completion record emitted by the previous
            # ``asyncio.gather([])`` path when no component opts into warmup.
            self.journal_sink.append_record(
                name="warmup_completed",
                data={"elapsed_ms": _elapsed_ms(started), "components": []},
            )
            return

        tasks = [
            asyncio.create_task(_warm_component(name, provider)) for name, provider in components
        ]
        try:
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        except asyncio.CancelledError:
            await _cancel_tasks(tasks)
            raise
        # Fail fast: once one warmup raises, cancel siblings still running so a
        # slow or hung provider can no longer hold ``Session.start()`` behind an
        # already-known failure (the previous serial path aborted immediately).
        if pending:
            await _cancel_tasks(pending)

        warmed: list[dict[str, Any]] = []
        failures: list[_ComponentWarmupError] = []
        for task in tasks:
            if task.cancelled():
                continue
            exc = task.exception()
            if isinstance(exc, _ComponentWarmupError):
                failures.append(exc)
                self.journal_sink.append_record(
                    kind=JournalRecordKind.CONTROL,
                    name="warmup_failed",
                    data={
                        "component": exc.component,
                        "elapsed_ms": exc.elapsed_ms,
                        "exc_type": type(exc.cause).__name__,
                    },
                )
                continue
            warmed.append(task.result())

        if failures:
            raise failures[0].cause

        self.journal_sink.append_record(
            name="warmup_completed",
            data={
                "elapsed_ms": _elapsed_ms(started),
                "components": warmed,
            },
        )


class _ComponentWarmupError(Exception):
    """Carry timing metadata for a failed component warmup.

    Raising (rather than returning) the failure lets ``asyncio.wait`` surface it
    via ``FIRST_EXCEPTION`` so siblings can be cancelled without waiting for a
    slow provider, while the original error is preserved on ``cause``.
    """

    def __init__(self, component: str, elapsed_ms: float, cause: BaseException) -> None:
        super().__init__(component)
        self.component = component
        self.elapsed_ms = elapsed_ms
        self.cause = cause


async def _warm_component(name: str, provider: Any) -> dict[str, Any]:
    component_started = time.perf_counter()
    try:
        await warmup_if_supported(provider)
    except Exception as exc:
        raise _ComponentWarmupError(name, _elapsed_ms(component_started), exc) from exc
    return {
        "component": name,
        "elapsed_ms": _elapsed_ms(component_started),
    }


async def _cancel_tasks(tasks: Iterable[asyncio.Task[Any]]) -> None:
    """Cancel ``tasks`` and await their settlement, ignoring their outcomes."""
    pending = [task for task in tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _elapsed_ms(started: float) -> float:
    """Return elapsed milliseconds with stable journal precision."""
    return round((time.perf_counter() - started) * 1000.0, 3)


__all__ = ["AudioResamplingWarmup", "WarmupComponent", "WarmupRunner"]
