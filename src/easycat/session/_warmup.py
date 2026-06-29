"""Session startup warmup execution."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

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
    ) -> None: ...


WarmupComponent = tuple[str, Any]


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
        if not components and select is not None:
            return

        results = await asyncio.gather(
            *(_warm_component(name, provider) for name, provider in components),
        )

        warmed: list[dict[str, Any]] = []
        failures: list[BaseException] = []
        for result in results:
            if "exception" in result:
                failures.append(result["exception"])
                self.journal_sink.append_record(
                    kind=JournalRecordKind.CONTROL,
                    name="warmup_failed",
                    data={
                        "component": result["component"],
                        "elapsed_ms": result["elapsed_ms"],
                        "exc_type": type(result["exception"]).__name__,
                    },
                )
                continue
            warmed.append(
                {
                    "component": result["component"],
                    "elapsed_ms": result["elapsed_ms"],
                }
            )

        if failures:
            raise failures[0]

        self.journal_sink.append_record(
            name="warmup_completed",
            data={
                "elapsed_ms": _elapsed_ms(started),
                "components": warmed,
            },
        )


async def _warm_component(name: str, provider: Any) -> dict[str, Any]:
    component_started = time.perf_counter()
    try:
        await warmup_if_supported(provider)
    except Exception as exc:
        return {
            "component": name,
            "elapsed_ms": _elapsed_ms(component_started),
            "exception": exc,
        }
    return {
        "component": name,
        "elapsed_ms": _elapsed_ms(component_started),
    }


def _elapsed_ms(started: float) -> float:
    """Return elapsed milliseconds with stable journal precision."""
    return round((time.perf_counter() - started) * 1000.0, 3)


__all__ = ["WarmupComponent", "WarmupRunner"]
