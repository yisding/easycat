from __future__ import annotations

# ruff: noqa: F401
import asyncio
import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from easycat import _observability as observability

REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeSpan:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False


class _FakeTracer:
    def __init__(self) -> None:
        self.started: list[tuple[str, dict[str, object]]] = []

    def start_as_current_span(self, name: str, attributes: dict[str, object]):
        self.started.append((name, attributes))
        return _FakeSpan()


class _FakeCounter:
    def __init__(self) -> None:
        self.adds: list[tuple[int | float, dict[str, object]]] = []

    def add(self, value: int | float, attributes: dict[str, object]) -> None:
        self.adds.append((value, attributes))


class _FakeHistogram:
    def __init__(self) -> None:
        self.records: list[tuple[int | float, dict[str, object]]] = []

    def record(self, value: int | float, attributes: dict[str, object]) -> None:
        self.records.append((value, attributes))


class _FakeObservableGauge:
    def __init__(self, callbacks: list[object]) -> None:
        self._callbacks = callbacks

    def collect(self) -> list[tuple[int | float, dict[str, object]]]:
        observations: list[tuple[int | float, dict[str, object]]] = []
        for callback in self._callbacks:
            observations.extend(callback(None))
        return observations


class _FakeMeter:
    def __init__(self) -> None:
        self.counters: dict[str, _FakeCounter] = {}
        self.histograms: dict[str, _FakeHistogram] = {}
        self.gauges: dict[str, _FakeObservableGauge] = {}

    def create_counter(self, name: str) -> _FakeCounter:
        counter = _FakeCounter()
        self.counters[name] = counter
        return counter

    def create_histogram(self, name: str) -> _FakeHistogram:
        histogram = _FakeHistogram()
        self.histograms[name] = histogram
        return histogram

    def create_observable_gauge(
        self,
        name: str,
        callbacks: list[object] | None = None,
    ) -> _FakeObservableGauge:
        gauge = _FakeObservableGauge(callbacks or [])
        self.gauges[name] = gauge
        return gauge


def _clear_observability_state() -> None:
    observability._COUNTERS.clear()
    observability._HISTOGRAMS.clear()
    observability._GAUGES.clear()
    observability._GAUGE_VALUES.clear()
    observability._get_meter.cache_clear()
    observability._get_tracer.cache_clear()
    with observability._ACTIVE_SESSIONS_LOCK:
        observability._ACTIVE_SESSIONS = 0


@pytest.fixture(autouse=True)
def reset_observability_state() -> Iterator[None]:
    _clear_observability_state()
    try:
        yield
    finally:
        _clear_observability_state()


def _make_run_ctx() -> object:
    from easycat.runtime.context import RunContext

    return RunContext(run_id="r1", session_id="s1", runtime_mode="chained_pipeline")


def _make_turn_ctx() -> object:
    from easycat._turn_context import TurnContext
    from easycat.cancel import CancelToken

    return TurnContext(turn_id="turn-1", cancel_token=CancelToken())


_LATENCY_DOC = REPO_ROOT / "docs/latency.md"

_LATENCY_DEFAULT_ROW = re.compile(
    r"^\| `(?P<cls>\w+)\.(?P<field>\w+)` \| `(?P<value>[0-9]+(?:\.[0-9]+)?)` \|",
    re.MULTILINE,
)


def _latency_config_classes() -> dict[str, type]:
    from easycat.integrations.agents import AgentRunnerConfig
    from easycat.session._types import SessionConfig
    from easycat.smart_turn import SmartTurnConfig
    from easycat.turn_manager import TurnManagerConfig
    from easycat.vad import VADConfig

    return {
        "AgentRunnerConfig": AgentRunnerConfig,
        "SessionConfig": SessionConfig,
        "SmartTurnConfig": SmartTurnConfig,
        "TurnManagerConfig": TurnManagerConfig,
        "VADConfig": VADConfig,
    }
