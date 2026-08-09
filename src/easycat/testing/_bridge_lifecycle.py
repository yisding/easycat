"""Internal lifecycle scenario suite for EasyCat's built-in agent bridges.

The public :class:`easycat.testing.AgentBridgeContractSuite` is deliberately
limited to behavior a third-party bridge can expose through its portable
factory.  This module owns the stronger first-party harness: SDK-specific
drivers translate controlled faults and cleanup probes into the normalized
observations asserted here.

The module is private and is not re-exported from :mod:`easycat.testing`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Protocol, TypeVar

import pytest

from easycat.integrations.agents._text_stream import AgentTextStream
from easycat.integrations.agents.base import AgentBridgeEvent, FrameworkStateSnapshot
from easycat.testing.contracts import ContractSuite

_ObservationT = TypeVar("_ObservationT")
BridgeLifecycleScenario = Literal[
    "interruption_prior_turn_isolation",
    "recorder_transient_cleanup",
    "stream_close_cleanup",
    "tool_inflight_cancellation_drain",
    "unknown_event_tolerance",
]
ALL_BRIDGE_LIFECYCLE_SCENARIOS: frozenset[BridgeLifecycleScenario] = frozenset(
    {
        "interruption_prior_turn_isolation",
        "recorder_transient_cleanup",
        "stream_close_cleanup",
        "tool_inflight_cancellation_drain",
        "unknown_event_tolerance",
    }
)


@dataclass(frozen=True)
class NormalizedHistoryEntry:
    """Framework-neutral history entry exposed by a lifecycle driver."""

    role: str
    text: str


@dataclass(frozen=True)
class NormalizedLifecycleState:
    """State that every driver must be able to inspect without SDK internals."""

    history: tuple[NormalizedHistoryEntry, ...] = ()
    active_streams: int = 0
    transient_items: int = 0


@dataclass(frozen=True)
class UnknownEventObservation:
    """Events visible after the driver injects malformed and unknown input."""

    events: tuple[AgentBridgeEvent, ...]


@dataclass(frozen=True)
class ToolCancellationObservation:
    """Normalized tool-drain and delivered-text state around cancellation."""

    events_before_cancel: tuple[AgentBridgeEvent, ...]
    events_after_cancel: tuple[AgentBridgeEvent, ...]
    tool_phases_before_cancel: tuple[str, ...]
    tool_phases_after_cancel: tuple[str, ...]
    committed_assistant_text: str
    inner_stream_close_calls: int


@dataclass(frozen=True)
class StreamCloseObservation:
    """Innermost-stream effects after the consumer closes the bridge stream."""

    inner_stream_close_calls: int
    running_work_cancelled: bool


@dataclass(frozen=True)
class RecorderCleanupObservation:
    """Recorder and bridge-transient state after closing a live stream."""

    entered_cursor_ids: tuple[str, ...]
    exited_cursor_ids: tuple[str, ...]
    transient_items_after_close: int
    inner_stream_close_calls: int


@dataclass(frozen=True)
class HistoryIsolationObservation:
    """History projection before and after an empty current turn is interrupted."""

    prior_history_before: tuple[NormalizedHistoryEntry, ...]
    prior_history_after: tuple[NormalizedHistoryEntry, ...]


class BridgeLifecycleScenarioDriver(Protocol):
    """Capability driver consumed by :class:`BridgeLifecycleScenarioSuite`.

    A built-in SDK driver owns injection details.  It must return direct
    observations (events, cursor ids, close counts, and normalized history),
    leaving the pass/fail policy in the shared suite.
    """

    async def observe_unknown_event_tolerance(
        self, *, valid_text: str
    ) -> UnknownEventObservation: ...

    async def observe_tool_inflight_cancellation(
        self, *, delivered_text: str
    ) -> ToolCancellationObservation: ...

    async def observe_stream_close_cleanup(self) -> StreamCloseObservation: ...

    async def observe_recorder_transient_cleanup(self) -> RecorderCleanupObservation: ...

    async def observe_interruption_history_isolation(
        self, *, prior_user_text: str, prior_assistant_text: str
    ) -> HistoryIsolationObservation: ...

    def normalized_state(self) -> NormalizedLifecycleState: ...

    def snapshot_state(self) -> FrameworkStateSnapshot: ...

    def reset(self) -> None: ...


class BridgeLifecycleScenarioSuite(ContractSuite):
    """Shared lifecycle assertions for first-party bridge capability drivers."""

    pytestmark: ClassVar[list[Any]] = [pytest.mark.contract]

    driver_factory: ClassVar[Callable[[], BridgeLifecycleScenarioDriver] | None] = None
    applicable_scenarios: ClassVar[frozenset[BridgeLifecycleScenario]] = (
        ALL_BRIDGE_LIFECYCLE_SCENARIOS
    )
    valid_text: ClassVar[str] = "valid response after unknown input"
    delivered_text: ClassVar[str] = "delivered partial"
    prior_user_text: ClassVar[str] = "prior question"
    prior_assistant_text: ClassVar[str] = "prior answer"

    @pytest.fixture
    def driver(self) -> BridgeLifecycleScenarioDriver:
        return self.build_driver()

    @classmethod
    def build_driver(cls) -> BridgeLifecycleScenarioDriver:
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

    async def test_unknown_event_tolerance(self, driver: BridgeLifecycleScenarioDriver) -> None:
        self._require_scenario("unknown_event_tolerance")
        observation = await self._observe(
            driver.observe_unknown_event_tolerance(valid_text=self.valid_text),
            scenario="unknown_event_tolerance",
        )

        assert [event.kind for event in observation.events] == ["text_delta", "done"]
        assert observation.events[0].text == self.valid_text
        assert observation.events[-1].text == self.valid_text
        self._assert_postconditions(driver)

    async def test_tool_inflight_cancellation_drain(
        self, driver: BridgeLifecycleScenarioDriver
    ) -> None:
        self._require_scenario("tool_inflight_cancellation_drain")
        observation = await self._observe(
            driver.observe_tool_inflight_cancellation(delivered_text=self.delivered_text),
            scenario="tool_inflight_cancellation_drain",
        )

        before_text = AgentTextStream()
        for event in observation.events_before_cancel:
            before_text.apply(event)
        assert before_text.text == self.delivered_text
        assert all(event.kind != "done" for event in observation.events_before_cancel)
        assert all(
            event.kind not in {"text_delta", "text_replace"}
            for event in observation.events_after_cancel
        )
        assert observation.tool_phases_before_cancel
        assert observation.tool_phases_before_cancel[0] == "start"
        assert "result" not in observation.tool_phases_before_cancel
        assert observation.tool_phases_after_cancel
        assert observation.tool_phases_after_cancel[-1] == "result"
        assert all(phase in {"delta", "result"} for phase in observation.tool_phases_after_cancel)
        assert observation.committed_assistant_text == self.delivered_text
        assert observation.inner_stream_close_calls == 1
        self._assert_postconditions(driver)

    async def test_stream_close_cleanup(self, driver: BridgeLifecycleScenarioDriver) -> None:
        self._require_scenario("stream_close_cleanup")
        observation = await self._observe(
            driver.observe_stream_close_cleanup(),
            scenario="stream_close_cleanup",
        )

        assert observation.inner_stream_close_calls == 1
        assert observation.running_work_cancelled is True
        self._assert_postconditions(driver)

    async def test_recorder_transient_cleanup(self, driver: BridgeLifecycleScenarioDriver) -> None:
        self._require_scenario("recorder_transient_cleanup")
        observation = await self._observe(
            driver.observe_recorder_transient_cleanup(),
            scenario="recorder_transient_cleanup",
        )

        assert Counter(observation.entered_cursor_ids) == Counter(observation.exited_cursor_ids)
        assert observation.entered_cursor_ids
        assert observation.transient_items_after_close == 0
        assert observation.inner_stream_close_calls == 1
        self._assert_postconditions(driver)

    async def test_interruption_prior_turn_isolation(
        self, driver: BridgeLifecycleScenarioDriver
    ) -> None:
        self._require_scenario("interruption_prior_turn_isolation")
        observation = await self._observe(
            driver.observe_interruption_history_isolation(
                prior_user_text=self.prior_user_text,
                prior_assistant_text=self.prior_assistant_text,
            ),
            scenario="interruption_prior_turn_isolation",
        )
        expected = (
            NormalizedHistoryEntry(role="user", text=self.prior_user_text),
            NormalizedHistoryEntry(role="assistant", text=self.prior_assistant_text),
        )

        assert observation.prior_history_before == expected
        assert observation.prior_history_after == expected
        self._assert_postconditions(driver)

    def _require_scenario(self, scenario: BridgeLifecycleScenario) -> None:
        if scenario not in self.applicable_scenarios:
            pytest.skip(f"bridge lifecycle matrix marks {scenario} not applicable")

    async def _observe(
        self, awaitable: Awaitable[_ObservationT], *, scenario: str
    ) -> _ObservationT:
        try:
            async with asyncio.timeout(self.event_timeout):
                return await awaitable
        except TimeoutError:
            pytest.fail(
                f"{scenario} did not complete within {self.event_timeout}s",
                pytrace=False,
            )

    @staticmethod
    def _assert_postconditions(driver: BridgeLifecycleScenarioDriver) -> None:
        before_reset = driver.snapshot_state()
        assert isinstance(before_reset, FrameworkStateSnapshot)
        json.dumps(before_reset.fields)

        driver.reset()
        after_reset = driver.snapshot_state()
        assert isinstance(after_reset, FrameworkStateSnapshot)
        json.dumps(after_reset.fields)
        assert driver.normalized_state() == NormalizedLifecycleState()
