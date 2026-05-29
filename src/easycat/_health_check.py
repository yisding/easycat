"""Provider health check infrastructure.

Defines a HealthCheckable protocol and a periodic health checker that
detects stale connections before they fail.

Detection alone is not remediation: a stale provider would otherwise just
emit an ``Error`` event every ``interval`` seconds forever. To turn this
into an actionable signal the checker tracks a consecutive-failure streak
and, once it crosses ``failure_threshold``, fires an ``on_unhealthy``
callback exactly once so the owner can reconnect/teardown the provider.
An ``on_recovered`` callback fires when a subsequent check succeeds.
Error events are de-duplicated to the healthy->unhealthy transition so a
permanently stale provider does not spam the event bus.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from easycat.runtime.capabilities import HealthCheckable

logger = logging.getLogger(__name__)

# Sync or async, takes the provider name. Mirrors ReconnectingWebSocket's
# callback style so owners can hook recovery the same way as on_give_up.
HealthCallback = Callable[[str], Coroutine[Any, Any, None]] | Callable[[str], None]


class PeriodicHealthChecker:
    """Periodically pings providers to detect stale connections.

    On the healthy->unhealthy transition (after ``failure_threshold``
    consecutive failures) it emits a single ``Error`` event and invokes the
    optional ``on_unhealthy`` callback so the owner can drive recovery
    (reconnect/teardown). When a later check succeeds it invokes the optional
    ``on_recovered`` callback. Repeated failures while already unhealthy do
    not re-emit the event, avoiding an Error every ``interval`` seconds.
    """

    def __init__(
        self,
        provider: HealthCheckable,
        *,
        interval: float = 30.0,
        provider_name: str = "unknown",
        event_bus: Any | None = None,
        failure_threshold: int = 1,
        on_unhealthy: HealthCallback | None = None,
        on_recovered: HealthCallback | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self._provider = provider
        self._interval = interval
        self._provider_name = provider_name
        self._event_bus = event_bus
        self._failure_threshold = failure_threshold
        self._on_unhealthy = on_unhealthy
        self._on_recovered = on_recovered
        self._task: asyncio.Task[None] | None = None
        self._running = False
        # Consecutive failures since the last healthy check.
        self._failure_streak = 0
        # True once the streak crossed the threshold; reset on recovery. Used
        # to de-duplicate Error emissions and fire callbacks on transitions.
        self._unhealthy = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_unhealthy(self) -> bool:
        """True once the failure streak has crossed ``failure_threshold``."""
        return self._unhealthy

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
        except Exception as exc:
            await self._record_failure(str(exc))
            return False
        if not healthy:
            await self._record_failure("health check returned unhealthy")
            return False
        await self._record_success()
        return True

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

    async def _record_success(self) -> None:
        """Reset the failure streak and fire recovery hooks on transition."""
        self._failure_streak = 0
        if self._unhealthy:
            self._unhealthy = False
            logger.info("Health check recovered for %s", self._provider_name)
            if self._on_recovered is not None:
                await self._invoke_callback(self._on_recovered)

    async def _record_failure(self, reason: str) -> None:
        """Advance the failure streak and escalate on the unhealthy transition."""
        self._failure_streak += 1
        logger.warning(
            "Health check failed for %s (%d/%d): %s",
            self._provider_name,
            self._failure_streak,
            self._failure_threshold,
            reason,
        )
        # Only escalate once, on the healthy->unhealthy transition, so a
        # permanently stale provider does not emit an Error every interval.
        if self._unhealthy or self._failure_streak < self._failure_threshold:
            return
        self._unhealthy = True
        await self._emit_error(reason)
        if self._on_unhealthy is not None:
            await self._invoke_callback(self._on_unhealthy)

    async def _emit_error(self, reason: str) -> None:
        """Emit an Error event on the unhealthy transition, if a bus is set."""
        if self._event_bus is None:
            return
        from easycat.events import Error, ErrorStage

        await self._event_bus.emit(
            Error(
                exception=RuntimeError(f"Health check failed for {self._provider_name}: {reason}"),
                stage=ErrorStage.PIPELINE,
                provider=self._provider_name,
            )
        )

    async def _invoke_callback(self, callback: HealthCallback) -> None:
        """Invoke a sync or async health callback, swallowing errors."""
        try:
            result = callback(self._provider_name)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("Error in health check callback %s", callback)
