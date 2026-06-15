"""M8 emission tests for ``easycat.server.metrics`` (no socket needed).

These prove the SAME-PR registration is correct: every helper records through
``sanitize_attributes`` without raising ``ValueError`` on the registered
``easycat.server.*`` names and the three new labels, the ``easycat.route``
firewall rejects an off-template value BEFORE recording, and the enumerated
route-template set is internally consistent.
"""

from __future__ import annotations

import pytest

from easycat import _observability as observability
from easycat.server import metrics as server_metrics
from easycat.server.routes import ROUTE_TEMPLATES
from tests.observability._observability_helpers import _FakeMeter


@pytest.fixture(autouse=True)
def _reset_observability_instruments() -> None:
    # The observability layer caches instruments in module-level dicts. The
    # ``tests/observability`` autouse reset does NOT apply to ``tests/server``,
    # so clear them here to keep each fake-meter test isolated.
    observability._COUNTERS.clear()
    observability._HISTOGRAMS.clear()
    observability._GAUGES.clear()
    observability._GAUGE_VALUES.clear()


@pytest.fixture
def fake_meter(monkeypatch: pytest.MonkeyPatch) -> _FakeMeter:
    meter = _FakeMeter()
    monkeypatch.setattr(observability, "_get_meter", lambda: meter)
    monkeypatch.setattr(observability, "_get_tracer", lambda: None)
    monkeypatch.setattr(
        observability,
        "_make_observation",
        lambda value, attributes: (value, attributes),
    )
    return meter


# ── Same-PR registration: emission never raises on registered names/labels ──


def test_record_request_emits_count_and_duration(fake_meter: _FakeMeter) -> None:
    server_metrics.record_request("/health/ready", duration_s=0.012, server_state="serving")

    counter = fake_meter.counters["easycat.server.requests.total"]
    assert counter.adds == [
        (1, {"easycat.route": "/health/ready", "easycat.server_state": "serving"})
    ]
    histogram = fake_meter.histograms["easycat.server.request.duration"]
    assert histogram.records == [(0.012, {"easycat.route": "/health/ready"})]


def test_record_session_rejected_with_auth_result(fake_meter: _FakeMeter) -> None:
    server_metrics.record_session_rejected(server_state="serving", auth_result="invalid")

    counter = fake_meter.counters["easycat.server.sessions.rejected.total"]
    assert counter.adds == [
        (1, {"easycat.server_state": "serving", "easycat.auth_result": "invalid"})
    ]


def test_record_session_rejected_without_auth_result(fake_meter: _FakeMeter) -> None:
    server_metrics.record_session_rejected(server_state="draining")

    counter = fake_meter.counters["easycat.server.sessions.rejected.total"]
    assert counter.adds == [(1, {"easycat.server_state": "draining"})]


def test_observe_connections_active_emits_gauge(fake_meter: _FakeMeter) -> None:
    server_metrics.observe_connections_active(3, server_state="serving")

    gauge = fake_meter.gauges["easycat.server.connections.active"]
    assert gauge.collect() == [(3, {"easycat.server_state": "serving"})]


def test_observe_draining_emits_zero_one_gauge(fake_meter: _FakeMeter) -> None:
    server_metrics.observe_draining(True)
    gauge = fake_meter.gauges["easycat.server.draining"]
    assert gauge.collect() == [(1, {})]

    server_metrics.observe_draining(False)
    assert fake_meter.gauges["easycat.server.draining"].collect() == [(0, {})]


def test_emission_is_noop_without_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    # With no meter (no OTel SDK), sanitize_attributes still runs but the helpers
    # must not raise on the registered names/labels.
    monkeypatch.setattr(observability, "_get_meter", lambda: None)
    monkeypatch.setattr(observability, "_get_tracer", lambda: None)

    server_metrics.record_request("/metrics", duration_s=0.0, server_state="serving")
    server_metrics.record_session_rejected(server_state="serving", auth_result="missing")
    server_metrics.observe_connections_active(0, server_state="serving")
    server_metrics.observe_draining(False)


# ── easycat.route firewall: off-template values are rejected BEFORE recording ──


@pytest.mark.parametrize(
    "raw_route",
    [
        "/health/ready?token=secret",
        "/secret/123",
        "/plan/../etc/passwd",
        "ws://attacker/voice",
        "/metrics ",  # trailing space is not the template
    ],
)
def test_route_label_rejects_off_template_value(raw_route: str) -> None:
    with pytest.raises(ValueError, match="easycat.route must be an enumerated route template"):
        server_metrics._route_attrs(raw_route)


def test_record_request_rejects_off_template_route(monkeypatch: pytest.MonkeyPatch) -> None:
    # The raw path is rejected before any meter is touched.
    meter = _FakeMeter()
    monkeypatch.setattr(observability, "_get_meter", lambda: meter)
    with pytest.raises(ValueError, match="easycat.route must be an enumerated route template"):
        server_metrics.record_request(
            "/health/ready?token=x", duration_s=0.1, server_state="serving"
        )
    assert "easycat.server.requests.total" not in meter.counters


def test_every_enumerated_template_is_accepted() -> None:
    for template in ROUTE_TEMPLATES:
        assert server_metrics._route_attrs(template) == {"easycat.route": template}


def test_server_route_templates_mirror_routes_module() -> None:
    # The metric layer and the route layer share a single source of truth.
    assert server_metrics.SERVER_ROUTE_TEMPLATES is ROUTE_TEMPLATES


def test_server_metric_names_are_all_registered() -> None:
    for name in server_metrics.SERVER_METRIC_NAMES:
        assert name in observability.METRIC_DEFINITIONS


def test_new_server_label_keys_are_all_registered() -> None:
    for key in server_metrics.NEW_SERVER_LABEL_KEYS:
        assert key in observability.LOW_CARDINALITY_ATTRIBUTE_KEYS
