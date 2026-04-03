"""Provider health check infrastructure.

Defines a HealthCheckable protocol and a periodic health checker that
detects stale connections before they fail.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class HealthCheckable(Protocol):
    """Optional protocol that providers can implement for health checking."""

    async def health_check(self) -> bool:
        """Return True if the provider is healthy, False otherwise."""
        ...


class PeriodicHealthChecker:
    """Periodically pings providers to detect stale connections.

    Emits Error events on health check failures via the event bus.
    """

    def __init__(
        self,
        provider: HealthCheckable,
        *,
        interval: float = 30.0,
        provider_name: str = "unknown",
        event_bus: Any | None = None,
    ) -> None:
        self._provider = provider
        self._interval = interval
        self._provider_name = provider_name
        self._event_bus = event_bus
        self._task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        """Start the periodic health check loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the periodic health check loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass  # Expected: we just cancelled the task above
        self._task = None

    async def check_once(self) -> bool:
        """Run a single health check. Returns True if healthy."""
        try:
            healthy = await self._provider.health_check()
            if not healthy:
                await self._report_failure("health check returned unhealthy")
            return healthy
        except Exception as exc:
            await self._report_failure(str(exc))
            return False

    async def _run(self) -> None:
        """Main health check loop."""
        try:
            while self._running:
                await asyncio.sleep(self._interval)
                if not self._running:
                    break
                await self.check_once()
        except asyncio.CancelledError:
            pass  # Graceful shutdown via stop()

    async def _report_failure(self, reason: str) -> None:
        """Log and optionally emit an event on health check failure."""
        logger.warning("Health check failed for %s: %s", self._provider_name, reason)
        if self._event_bus is not None:
            from easycat.events import Error, ErrorStage

            await self._event_bus.emit(
                Error(
                    exception=RuntimeError(
                        f"Health check failed for {self._provider_name}: {reason}"
                    ),
                    stage=ErrorStage.PIPELINE,
                    provider=self._provider_name,
                )
            )
