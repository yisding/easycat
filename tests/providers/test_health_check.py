"""Tests for provider health check infrastructure."""

from __future__ import annotations

import asyncio
import gc
import logging

import pytest

from easycat._health_check import HealthCheckable, PeriodicHealthChecker
from easycat.events import Error, ErrorStage, EventBus
from easycat.runtime.scope import RuntimeScope, RuntimeSupervisor


class HealthyProvider:
    async def health_check(self) -> bool:
        return True


class UnhealthyProvider:
    async def health_check(self) -> bool:
        return False


class FailingProvider:
    async def health_check(self) -> bool:
        raise ConnectionError("WebSocket stale")


class NotifyingHealthyProvider:
    def __init__(self) -> None:
        self.checked = asyncio.Event()

    async def health_check(self) -> bool:
        self.checked.set()
        return True


class NotifyingUnhealthyProvider:
    def __init__(self) -> None:
        self.checked = asyncio.Event()

    async def health_check(self) -> bool:
        self.checked.set()
        return False


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
        provider = NotifyingHealthyProvider()
        checker = PeriodicHealthChecker(
            provider,
            interval=0,
            provider_name="test",
        )
        checker.start()
        assert checker.is_running

        try:
            await asyncio.wait_for(provider.checked.wait(), timeout=0.5)
        finally:
            await checker.stop()
        assert not checker.is_running

    async def test_session_scope_owns_periodic_task(self):
        provider = NotifyingHealthyProvider()
        root = RuntimeScope.create_root(
            name="session",
            root_id="session:health-test",
            supervisor=RuntimeSupervisor(capacity=1),
            survivor_capacity=1,
        )
        checker = PeriodicHealthChecker(provider, interval=0, provider_name="test")
        checker.set_runtime_scope(root, name="test-health-check")
        checker.start()
        try:
            await asyncio.wait_for(provider.checked.wait(), timeout=0.5)
            task = checker._task
            assert task is not None
            assert checker._tasks.scope is not None
            assert checker._tasks.scope.parent is root
            assert checker._tasks.tasks() == (task,)
        finally:
            await checker.stop()
            await root.close()

    async def test_runtime_root_close_cancels_perpetual_periodic_task(self):
        provider = NotifyingHealthyProvider()
        root = RuntimeScope.create_root(
            name="session",
            root_id="session:health-root-close",
            supervisor=RuntimeSupervisor(capacity=1),
            survivor_capacity=1,
        )
        checker = PeriodicHealthChecker(provider, interval=0, provider_name="test")
        checker.set_runtime_scope(root, name="test-health-check")
        checker.start()
        await provider.checked.wait()
        task = checker._task
        assert task is not None

        await asyncio.wait_for(root.close(), timeout=0.5)

        assert task.cancelled()
        assert checker.is_running is False

    def test_start_without_running_loop_leaves_no_task_or_coroutine_warning(
        self,
        recwarn: pytest.WarningsRecorder,
    ) -> None:
        checker = PeriodicHealthChecker(HealthyProvider(), provider_name="test")

        with pytest.raises(RuntimeError, match="no running event loop"):
            checker.start()
        gc.collect()

        assert checker.is_running is False
        assert checker._task is None
        assert not [warning for warning in recwarn if issubclass(warning.category, RuntimeWarning)]

    async def test_stop_preserves_caller_cancellation_and_retains_pending_task(self):
        """Cancelling the owner must not be swallowed as the child's stop signal."""
        checker = PeriodicHealthChecker(HealthyProvider(), provider_name="test")
        release = asyncio.Event()
        child_cancelled = asyncio.Event()

        async def cancellation_resistant_task() -> None:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    child_cancelled.set()

        child = asyncio.create_task(cancellation_resistant_task())
        checker._tasks.adopt_task(child)
        checker._running = True
        checker._task = child
        stopping = asyncio.create_task(checker.stop())
        await child_cancelled.wait()

        try:
            stopping.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stopping

            assert checker._task is child
            assert checker.is_running is False
        finally:
            release.set()
            await child

        await checker.stop()
        assert checker._task is None

    async def test_periodic_detects_stale_connection(self):
        """Start a periodic checker that detects a stale (unhealthy) provider."""
        event_bus = EventBus()
        errors = []
        detected = asyncio.Event()

        async def handler(event):
            errors.append(event)
            detected.set()

        event_bus.subscribe(Error, handler)

        provider = NotifyingUnhealthyProvider()
        checker = PeriodicHealthChecker(
            provider,
            interval=0,
            provider_name="stale_ws",
            event_bus=event_bus,
        )
        checker.start()
        try:
            await asyncio.wait_for(detected.wait(), timeout=0.5)
        finally:
            await checker.stop()

        assert len(errors) >= 1

    async def test_error_subscriber_can_stop_checker_from_its_own_task(self):
        event_bus = EventBus()
        stopped = asyncio.Event()
        checker: PeriodicHealthChecker

        async def stop_from_error(_event: Error) -> None:
            await checker.stop()
            stopped.set()

        event_bus.subscribe(Error, stop_from_error)
        checker = PeriodicHealthChecker(
            UnhealthyProvider(),
            interval=0,
            provider_name="self-stopping",
            event_bus=event_bus,
        )
        checker.start()
        task = checker._task
        assert task is not None

        await asyncio.wait_for(stopped.wait(), timeout=0.5)
        await asyncio.wait_for(task, timeout=0.5)

        assert checker.is_running is False

    async def test_reentrant_stop_then_start_reuses_probe_loop(self):
        """stop()+start() from inside the Error emit must not spawn a 2nd loop."""
        event_bus = EventBus()
        restarted = asyncio.Event()
        checker: PeriodicHealthChecker

        class CountingUnhealthyProvider:
            def __init__(self) -> None:
                self.calls = 0
                self.probed_after_restart = asyncio.Event()

            async def health_check(self) -> bool:
                self.calls += 1
                if restarted.is_set():
                    self.probed_after_restart.set()
                return False

        async def stop_then_start(_event: Error) -> None:
            await checker.stop()
            checker.start()
            restarted.set()

        event_bus.subscribe(Error, stop_then_start)
        provider = CountingUnhealthyProvider()
        checker = PeriodicHealthChecker(
            provider,
            interval=0,
            provider_name="restarting",
            event_bus=event_bus,
        )
        checker.start()
        original = checker._task
        assert original is not None

        await asyncio.wait_for(restarted.wait(), timeout=0.5)

        # The winding-down loop was re-armed rather than replaced.
        assert checker.is_running is True
        assert checker._task is original
        assert not original.done()
        await asyncio.wait_for(provider.probed_after_restart.wait(), timeout=0.5)
        assert len(checker._tasks.tasks()) == 1

        await checker.stop()
        assert original.done()
        assert checker._task is None
        assert checker.is_running is False

    async def test_periodic_loop_logs_strict_error_handler_failure(
        self,
        caplog: pytest.LogCaptureFixture,
    ):
        event_bus = EventBus(handler_error_policy="raise")

        async def fail(_event: Error) -> None:
            raise RuntimeError("health handler failed")

        event_bus.subscribe(Error, fail)
        provider = NotifyingUnhealthyProvider()
        checker = PeriodicHealthChecker(
            provider,
            interval=0,
            provider_name="strict-provider",
            event_bus=event_bus,
        )

        with caplog.at_level(logging.ERROR, logger="easycat._health_check"):
            checker.start()
            await asyncio.wait_for(provider.checked.wait(), timeout=0.5)
            task = checker._task
            assert task is not None
            await asyncio.wait_for(task, timeout=0.5)

        assert "Periodic health check loop failed for strict-provider" in caplog.text
        assert "health handler failed" in caplog.text
        assert checker.is_running is False

        provider.checked.clear()
        checker.start()
        await asyncio.wait_for(provider.checked.wait(), timeout=0.5)
        assert checker.is_running is True
        await checker.stop()

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
        with pytest.raises(ValueError):
            PeriodicHealthChecker(UnhealthyProvider(), failure_threshold=0)
