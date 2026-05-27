"""Tests for provider health check infrastructure."""

from __future__ import annotations

import asyncio

from easycat._health_check import HealthCheckable, PeriodicHealthChecker
from easycat.events import Error, ErrorStage, EventBus


class HealthyProvider:
    async def health_check(self) -> bool:
        return True


class UnhealthyProvider:
    async def health_check(self) -> bool:
        return False


class FailingProvider:
    async def health_check(self) -> bool:
        raise ConnectionError("WebSocket stale")


class TestHealthCheckable:
    def test_protocol_detection(self):
        assert isinstance(HealthyProvider(), HealthCheckable)

    def test_non_provider_not_detected(self):
        class NoHealthCheck:
            pass

        assert not isinstance(NoHealthCheck(), HealthCheckable)


class TestPeriodicHealthChecker:
    async def test_check_once_healthy(self):
        checker = PeriodicHealthChecker(HealthyProvider(), provider_name="test")
        assert await checker.check_once() is True

    async def test_check_once_unhealthy(self):
        checker = PeriodicHealthChecker(UnhealthyProvider(), provider_name="test")
        assert await checker.check_once() is False

    async def test_check_once_exception(self):
        checker = PeriodicHealthChecker(FailingProvider(), provider_name="test")
        assert await checker.check_once() is False

    async def test_unhealthy_emits_error_event(self):
        event_bus = EventBus()
        errors = []

        async def handler(event):
            errors.append(event)

        event_bus.subscribe(Error, handler)

        checker = PeriodicHealthChecker(
            UnhealthyProvider(),
            provider_name="stale_ws",
            event_bus=event_bus,
        )
        await checker.check_once()

        assert len(errors) == 1
        assert errors[0].stage == ErrorStage.PIPELINE
        assert errors[0].provider == "stale_ws"

    async def test_exception_emits_error_event(self):
        event_bus = EventBus()
        errors = []

        async def handler(event):
            errors.append(event)

        event_bus.subscribe(Error, handler)

        checker = PeriodicHealthChecker(
            FailingProvider(),
            provider_name="broken_ws",
            event_bus=event_bus,
        )
        await checker.check_once()

        assert len(errors) == 1
        assert errors[0].stage == ErrorStage.PIPELINE
        assert errors[0].provider == "broken_ws"

    async def test_start_stop(self):
        checker = PeriodicHealthChecker(
            HealthyProvider(),
            interval=0.05,
            provider_name="test",
        )
        checker.start()
        assert checker.is_running

        await asyncio.sleep(0.15)
        await checker.stop()
        assert not checker.is_running

    async def test_periodic_detects_stale_connection(self):
        """Start a periodic checker that detects a stale (unhealthy) provider."""
        event_bus = EventBus()
        errors = []

        async def handler(event):
            errors.append(event)

        event_bus.subscribe(Error, handler)

        checker = PeriodicHealthChecker(
            UnhealthyProvider(),
            interval=0.05,
            provider_name="stale_ws",
            event_bus=event_bus,
        )
        checker.start()
        await asyncio.sleep(0.15)
        await checker.stop()

        assert len(errors) >= 1
