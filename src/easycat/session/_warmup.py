"""Session startup warmup execution."""

from __future__ import annotations

import time
from collections.abc import Sequence
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

    async def run(self) -> None:
        """Execute all supported ``warmup()`` hooks and record startup timing."""
        if not self.enabled:
            return

        started = time.perf_counter()
        warmed: list[dict[str, Any]] = []
        for name, provider in self.components:
            if warmupable(provider) is None:
                continue
            component_started = time.perf_counter()
            try:
                await warmup_if_supported(provider)
            except Exception as exc:
                self.journal_sink.append_record(
                    kind=JournalRecordKind.CONTROL,
                    name="warmup_failed",
                    data={
                        "component": name,
                        "elapsed_ms": _elapsed_ms(component_started),
                        "exc_type": type(exc).__name__,
                    },
                )
                raise
            warmed.append(
                {
                    "component": name,
                    "elapsed_ms": _elapsed_ms(component_started),
                }
            )

        self.journal_sink.append_record(
            name="warmup_completed",
            data={
                "elapsed_ms": _elapsed_ms(started),
                "components": warmed,
            },
        )


def _elapsed_ms(started: float) -> float:
    """Return elapsed milliseconds with stable journal precision."""
    return round((time.perf_counter() - started) * 1000.0, 3)


__all__ = ["WarmupComponent", "WarmupRunner"]
