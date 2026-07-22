"""``easycat.interruption.cutoff_latency`` emission on barge-in.

The histogram is declared in ``_observability.METRIC_DEFINITIONS`` and is
emitted by ``Session.cancel_turn`` once playback has actually been cleared on
the transport (barge-in initiation → transport clear). These tests drive a
barge-in through a Session wired with stub providers and assert the histogram
is recorded, and that a non-barge-in cancel does not record it.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from easycat import _observability as observability
from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.session import _session as session_module
from easycat.session._session import Session
from easycat.session._types import TurnState
from tests.session._session_core_helpers import _full_config


class _FakeHistogram:
    def __init__(self) -> None:
        self.records: list[tuple[float, dict[str, object]]] = []

    def record(self, value: float, attributes: dict[str, object]) -> None:
        self.records.append((value, attributes))


class _FakeCounter:
    def __init__(self) -> None:
        self.adds: list[tuple[float, dict[str, object]]] = []

    def add(self, value: float, attributes: dict[str, object]) -> None:
        self.adds.append((value, attributes))


class _FakeMeter:
    def __init__(self) -> None:
        self.histograms: dict[str, _FakeHistogram] = {}
        self.counters: dict[str, _FakeCounter] = {}

    def create_histogram(self, name: str) -> _FakeHistogram:
        histogram = _FakeHistogram()
        self.histograms[name] = histogram
        return histogram

    def create_counter(self, name: str) -> _FakeCounter:
        counter = _FakeCounter()
        self.counters[name] = counter
        return counter


@pytest.fixture
def fake_meter(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeMeter]:
    meter = _FakeMeter()
    original_get_meter = observability._get_meter
    observability._COUNTERS.clear()
    observability._HISTOGRAMS.clear()
    original_get_meter.cache_clear()
    monkeypatch.setattr(observability, "_get_meter", lambda: meter)
    try:
        yield meter
    finally:
        observability._COUNTERS.clear()
        observability._HISTOGRAMS.clear()
        original_get_meter.cache_clear()


async def test_barge_in_records_cutoff_latency(
    fake_meter: _FakeMeter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = Session(_full_config())
    # Scenario doc only: the barge-in cutoff path keys off ``self._turn`` +
    # ``barge_in=True``, not this attribute (Session has no ``_turn_state``).
    session._turn_state = TurnState.BOT_SPEAKING  # type: ignore[attr-defined]
    session._turn = TurnContext("cutoff-turn", CancelToken())
    monotonic = Mock(side_effect=(100.0, 100.25))
    monkeypatch.setattr(session_module, "time", SimpleNamespace(monotonic=monotonic))

    await session.cancel_turn(barge_in=True)

    histogram = fake_meter.histograms.get("easycat.interruption.cutoff_latency")
    assert histogram is not None, "cutoff-latency histogram was never recorded"
    assert len(histogram.records) == 1
    value, attributes = histogram.records[0]
    # Latency histograms use seconds across the observability surface.
    assert value == pytest.approx(0.25)
    assert attributes == {"easycat.surface": "vad"}


async def test_non_barge_in_cancel_does_not_record_cutoff_latency(
    fake_meter: _FakeMeter,
) -> None:
    session = Session(_full_config())
    # Scenario doc only: the barge-in cutoff path keys off ``self._turn`` +
    # ``barge_in=True``, not this attribute (Session has no ``_turn_state``).
    session._turn_state = TurnState.BOT_SPEAKING  # type: ignore[attr-defined]
    session._turn = TurnContext("cutoff-turn", CancelToken())

    await session.cancel_turn()

    assert "easycat.interruption.cutoff_latency" not in fake_meter.histograms
