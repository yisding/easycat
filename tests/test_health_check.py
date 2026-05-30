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

    async def test_failure_threshold_delays_escalation(self):
        event_bus = EventBus()
        errors = []
        event_bus.subscribe(Error, lambda e: errors.append(e))

        checker = PeriodicHealthChecker(
            UnhealthyProvider(),
            provider_name="stale_ws",
            event_bus=event_bus,
            failure_threshold=3,
        )

        await checker.check_once()
        assert checker.is_unhealthy is False
        assert errors == []

        await checker.check_once()
        assert checker.is_unhealthy is False
        assert errors == []

        await checker.check_once()
        assert checker.is_unhealthy is True
        assert len(errors) == 1

    async def test_error_emitted_once_on_transition(self):
        event_bus = EventBus()
        errors = []
        event_bus.subscribe(Error, lambda e: errors.append(e))

        checker = PeriodicHealthChecker(
            UnhealthyProvider(),
            provider_name="stale_ws",
            event_bus=event_bus,
        )

        for _ in range(5):
            await checker.check_once()

        # De-duplicated: only the healthy->unhealthy transition emits.
        assert len(errors) == 1

    async def test_on_unhealthy_callback_fires_once(self):
        calls = []

        async def on_unhealthy(name):
            calls.append(name)

        checker = PeriodicHealthChecker(
            UnhealthyProvider(),
            provider_name="stale_ws",
            on_unhealthy=on_unhealthy,
        )

        for _ in range(3):
            await checker.check_once()

        assert calls == ["stale_ws"]

    async def test_on_recovered_callback_after_unhealthy(self):
        recovered = []

        async def on_recovered(name):
            recovered.append(name)

        class FlakyProvider:
            def __init__(self) -> None:
                self.healthy = False

            async def health_check(self) -> bool:
                return self.healthy

        provider = FlakyProvider()
        checker = PeriodicHealthChecker(
            provider,
            provider_name="flaky_ws",
            on_recovered=on_recovered,
        )

        await checker.check_once()
        assert checker.is_unhealthy is True
        assert recovered == []

        provider.healthy = True
        await checker.check_once()
        assert checker.is_unhealthy is False
        assert recovered == ["flaky_ws"]

    async def test_sync_callback_supported(self):
        calls = []
        checker = PeriodicHealthChecker(
            UnhealthyProvider(),
            provider_name="stale_ws",
            on_unhealthy=lambda name: calls.append(name),
        )
        await checker.check_once()
        assert calls == ["stale_ws"]

    def test_invalid_failure_threshold(self):
        import pytest

        with pytest.raises(ValueError):
            PeriodicHealthChecker(UnhealthyProvider(), failure_threshold=0)
