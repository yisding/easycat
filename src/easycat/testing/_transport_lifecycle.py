"""Internal lifecycle scenario suite for EasyCat's built-in transports.

The public :class:`easycat.testing.TransportContractSuite` remains limited to
portable protocol behavior. Built-in capability drivers translate concrete
backend races and faults into the normalized observations asserted here. This
module is private and is not re-exported from :mod:`easycat.testing`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Protocol, TypeVar

import pytest

from easycat.testing.contracts import ContractSuite

_ObservationT = TypeVar("_ObservationT")
TransportLifecycleScenario = Literal[
    "connect_leadership_race",
    "degraded_emission",
    "disconnect_during_connect",
    "interrupted_disconnect_publication",
    "late_frames",
    "mid_stream_teardown",
    "queue_overflow",
    "startup_rollback",
]
ALL_TRANSPORT_LIFECYCLE_SCENARIOS: frozenset[TransportLifecycleScenario] = frozenset(
    {
        "connect_leadership_race",
        "degraded_emission",
        "disconnect_during_connect",
        "interrupted_disconnect_publication",
        "late_frames",
        "mid_stream_teardown",
        "queue_overflow",
        "startup_rollback",
    }
)


@dataclass(frozen=True)
class NormalizedTransportLifecycleState:
    """Framework-neutral owned state exposed after each scenario."""

    connected: bool = False
    active_generation: str | None = None
    owned_work: int = 0
    queued_frames: int = 0
    retained_cleanup: int = 0


@dataclass(frozen=True)
class ConnectLeadershipObservation:
    backend_start_calls: int
    caller_generations: tuple[str, ...]
    connected_publications: tuple[str, ...]


@dataclass(frozen=True)
class NormalizedDegradedEvent:
    provider: str
    reason: str
    detail: str
    fatal: bool


@dataclass(frozen=True)
class DegradedEmissionObservation:
    events: tuple[NormalizedDegradedEvent, ...]


@dataclass(frozen=True)
class DisconnectDuringConnectObservation:
    connect_cancelled: bool
    backend_close_calls: int
    connected_publications: tuple[str, ...]


@dataclass(frozen=True)
class InterruptedDisconnectObservation:
    caller_cancelled: bool
    connected_during_retained_cleanup: bool
    retained_cleanup_during_cancel: int
    backend_close_calls: int
    lifecycle_publications: tuple[str | None, ...]


@dataclass(frozen=True)
class LateFrameObservation:
    stale_generation: str
    active_generation: str
    stale_accepted: bool
    active_accepted: bool
    delivered_frames: tuple[str, ...]


@dataclass(frozen=True)
class MidStreamTeardownObservation:
    receiver_terminated: bool
    backend_close_calls: int
    owned_work_after_disconnect: int


@dataclass(frozen=True)
class QueueOverflowObservation:
    accepted: tuple[bool, ...]
    dropped_frames: int
    degraded_reasons: tuple[str, ...]


@dataclass(frozen=True)
class StartupRollbackObservation:
    startup_error: str
    live_resources_after_failure: int
    connected_after_failure: bool
    retry_generation: str
    backend_start_calls: int
    backend_close_calls: int


class TransportLifecycleScenarioDriver(Protocol):
    """Capability driver consumed by :class:`TransportLifecycleScenarioSuite`."""

    async def observe_connect_leadership_race(self) -> ConnectLeadershipObservation: ...

    async def observe_degraded_emission(self) -> DegradedEmissionObservation: ...

    async def observe_disconnect_during_connect(
        self,
    ) -> DisconnectDuringConnectObservation: ...

    async def observe_interrupted_disconnect_publication(
        self,
    ) -> InterruptedDisconnectObservation: ...

    async def observe_late_frames(self) -> LateFrameObservation: ...

    async def observe_mid_stream_teardown(self) -> MidStreamTeardownObservation: ...

    async def observe_queue_overflow(self) -> QueueOverflowObservation: ...

    async def observe_startup_rollback(self) -> StartupRollbackObservation: ...

    def normalized_state(self) -> NormalizedTransportLifecycleState: ...

    def snapshot_state(self) -> dict[str, Any]: ...

    def reset(self) -> None: ...


class TransportLifecycleScenarioSuite(ContractSuite):
    """Shared lifecycle assertions for first-party transport capability drivers."""

    pytestmark: ClassVar[list[Any]] = [pytest.mark.contract]
    driver_factory: ClassVar[Callable[[], TransportLifecycleScenarioDriver] | None] = None
    applicable_scenarios: ClassVar[frozenset[TransportLifecycleScenario]] = (
        ALL_TRANSPORT_LIFECYCLE_SCENARIOS
    )

    @pytest.fixture
    def driver(self) -> TransportLifecycleScenarioDriver:
        return self.build_driver()

    @classmethod
    def build_driver(cls) -> TransportLifecycleScenarioDriver:
        factory = inspect.getattr_static(cls, "driver_factory", None)
        if factory is None:
            pytest.fail(
                f"{cls.__name__} must set `driver_factory` to a zero-argument callable",
                pytrace=False,
            )
        if isinstance(factory, staticmethod | classmethod):
            assert cls.driver_factory is not None
            return cls.driver_factory()
        return factory()

    async def test_connect_leadership_race(self, driver: TransportLifecycleScenarioDriver) -> None:
        self._require_scenario("connect_leadership_race")
        observation = await self._observe(
            driver.observe_connect_leadership_race(), scenario="connect_leadership_race"
        )
        assert observation.backend_start_calls == 1
        assert len(observation.caller_generations) == 2
        assert len(set(observation.caller_generations)) == 1
        assert observation.connected_publications == (observation.caller_generations[0],)
        self._assert_postconditions(driver)

    async def test_degraded_emission(self, driver: TransportLifecycleScenarioDriver) -> None:
        self._require_scenario("degraded_emission")
        observation = await self._observe(
            driver.observe_degraded_emission(), scenario="degraded_emission"
        )
        assert observation.events == (
            NormalizedDegradedEvent(
                provider="offline-lifecycle-model",
                reason="model_fault",
                detail="controlled lifecycle fault",
                fatal=False,
            ),
        )
        self._assert_postconditions(driver)

    async def test_disconnect_during_connect(
        self, driver: TransportLifecycleScenarioDriver
    ) -> None:
        self._require_scenario("disconnect_during_connect")
        observation = await self._observe(
            driver.observe_disconnect_during_connect(), scenario="disconnect_during_connect"
        )
        assert observation.connect_cancelled is True
        assert observation.backend_close_calls == 1
        assert observation.connected_publications == ()
        self._assert_postconditions(driver)

    async def test_interrupted_disconnect_publication(
        self, driver: TransportLifecycleScenarioDriver
    ) -> None:
        self._require_scenario("interrupted_disconnect_publication")
        observation = await self._observe(
            driver.observe_interrupted_disconnect_publication(),
            scenario="interrupted_disconnect_publication",
        )
        assert observation.caller_cancelled is True
        assert observation.connected_during_retained_cleanup is True
        assert observation.retained_cleanup_during_cancel == 1
        assert observation.backend_close_calls == 1
        assert len(observation.lifecycle_publications) == 2
        assert observation.lifecycle_publications[0] is not None
        assert observation.lifecycle_publications[-1] is None
        self._assert_postconditions(driver)

    async def test_late_frames(self, driver: TransportLifecycleScenarioDriver) -> None:
        self._require_scenario("late_frames")
        observation = await self._observe(driver.observe_late_frames(), scenario="late_frames")
        assert observation.stale_generation != observation.active_generation
        assert observation.stale_accepted is False
        assert observation.active_accepted is True
        assert observation.delivered_frames == ("fresh",)
        self._assert_postconditions(driver)

    async def test_mid_stream_teardown(self, driver: TransportLifecycleScenarioDriver) -> None:
        self._require_scenario("mid_stream_teardown")
        observation = await self._observe(
            driver.observe_mid_stream_teardown(), scenario="mid_stream_teardown"
        )
        assert observation.receiver_terminated is True
        assert observation.backend_close_calls == 1
        assert observation.owned_work_after_disconnect == 0
        self._assert_postconditions(driver)

    async def test_queue_overflow(self, driver: TransportLifecycleScenarioDriver) -> None:
        self._require_scenario("queue_overflow")
        observation = await self._observe(
            driver.observe_queue_overflow(), scenario="queue_overflow"
        )
        assert observation.accepted == (True, False)
        assert observation.dropped_frames == 1
        assert observation.degraded_reasons == ("inbound_queue_full",)
        self._assert_postconditions(driver)

    async def test_startup_rollback(self, driver: TransportLifecycleScenarioDriver) -> None:
        self._require_scenario("startup_rollback")
        observation = await self._observe(
            driver.observe_startup_rollback(), scenario="startup_rollback"
        )
        assert observation.startup_error == "controlled startup failure"
        assert observation.live_resources_after_failure == 0
        assert observation.connected_after_failure is False
        assert observation.retry_generation
        assert observation.backend_start_calls == 2
        assert observation.backend_close_calls == 2
        self._assert_postconditions(driver)

    async def _observe(
        self,
        awaitable: Awaitable[_ObservationT],
        *,
        scenario: TransportLifecycleScenario,
    ) -> _ObservationT:
        try:
            async with asyncio.timeout(self.event_timeout):
                return await awaitable
        except TimeoutError:
            pytest.fail(
                f"{scenario} did not complete within {self.event_timeout}s",
                pytrace=False,
            )

    def _require_scenario(self, scenario: TransportLifecycleScenario) -> None:
        if scenario not in self.applicable_scenarios:
            pytest.skip(f"transport lifecycle matrix marks {scenario} not applicable")

    @staticmethod
    def _assert_postconditions(driver: TransportLifecycleScenarioDriver) -> None:
        assert driver.normalized_state() == NormalizedTransportLifecycleState()
        json.dumps(driver.snapshot_state())
        driver.reset()
        assert driver.normalized_state() == NormalizedTransportLifecycleState()
        json.dumps(driver.snapshot_state())
