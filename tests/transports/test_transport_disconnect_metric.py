"""``easycat.transport.disconnects.total`` emission on abnormal disconnects.

The counter is declared in ``_observability.METRIC_DEFINITIONS`` and is
incremented by ``AudioQueueMixin._record_transport_disconnect`` — invoked at
each transport's abnormal-disconnect detection site (a ``ConnectionClosedError``
in WebSocket/Twilio, an ICE ``failed``/``disconnected`` transition in WebRTC,
and any fatal ``_emit_degraded`` that forces the session down). Clean closes
(``disconnect()``, ``ConnectionClosedOK``, a WebRTC ``closed`` state) are not
counted.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from easycat import _observability as observability
from easycat.transports._base import AudioQueueMixin


class _FakeCounter:
    def __init__(self) -> None:
        self.adds: list[tuple[float, dict[str, object]]] = []

    def add(self, value: float, attributes: dict[str, object]) -> None:
        self.adds.append((value, attributes))


class _FakeMeter:
    def __init__(self) -> None:
        self.counters: dict[str, _FakeCounter] = {}

    def create_counter(self, name: str) -> _FakeCounter:
        counter = _FakeCounter()
        self.counters[name] = counter
        return counter


class _StubQueueTransport(AudioQueueMixin):
    transport_kind = "stub"

    def __init__(self) -> None:
        self._init_audio_queue(4)


@pytest.fixture
def fake_meter(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeMeter]:
    meter = _FakeMeter()
    original_get_meter = observability._get_meter
    observability._COUNTERS.clear()
    original_get_meter.cache_clear()
    monkeypatch.setattr(observability, "_get_meter", lambda: meter)
    try:
        yield meter
    finally:
        observability._COUNTERS.clear()
        original_get_meter.cache_clear()


def _disconnect_counter(meter: _FakeMeter) -> _FakeCounter | None:
    return meter.counters.get("easycat.transport.disconnects.total")


def test_record_transport_disconnect_increments_with_transport_label(
    fake_meter: _FakeMeter,
) -> None:
    transport = _StubQueueTransport()

    transport._record_transport_disconnect("abnormal drop")

    counter = _disconnect_counter(fake_meter)
    assert counter is not None
    assert len(counter.adds) == 1
    value, attributes = counter.adds[0]
    assert value == 1
    assert attributes == {"easycat.transport": "stub"}


def test_fatal_degraded_counts_as_disconnect_without_bus(fake_meter: _FakeMeter) -> None:
    """A fatal degradation forces the session down, so it counts even when no
    event bus is attached (the counter fires before the bus/loop guard)."""
    transport = _StubQueueTransport()
    assert transport._event_bus is None

    transport._emit_degraded("control_codec_poisoned", "boom", fatal=True)

    counter = _disconnect_counter(fake_meter)
    assert counter is not None
    assert len(counter.adds) == 1
    assert counter.adds[0][1] == {"easycat.transport": "stub"}


def test_non_fatal_degraded_does_not_count_as_disconnect(fake_meter: _FakeMeter) -> None:
    transport = _StubQueueTransport()

    transport._emit_degraded("inbound_queue_full", "dropped a frame", fatal=False)

    assert _disconnect_counter(fake_meter) is None
