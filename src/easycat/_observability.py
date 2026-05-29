"""Optional OpenTelemetry-facing observability helpers.

Core keeps this module dependency-light: if ``opentelemetry-api`` is absent,
all span and metric operations are no-ops. Host applications that configure an
OTel SDK/exporter get spans and metrics through the standard API.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator, Mapping
from typing import Any, Literal

MetricKind = Literal["counter", "histogram", "observable_gauge"]

INSTRUMENTATION_NAME = "easycat"

SPAN_NAMES = frozenset(
    {
        "easycat.session",
        "easycat.transport.receive",
        "easycat.vad.detect",
        "easycat.stt.stream",
        "easycat.turn.commit",
        "easycat.agent.invoke",
        "easycat.agent.tool",
        "easycat.tts.synthesize",
        "easycat.transport.send",
        "easycat.journal.append",
    }
)

METRIC_DEFINITIONS: Mapping[str, MetricKind] = {
    "easycat.turn.latency": "histogram",
    "easycat.stage.latency": "histogram",
    "easycat.journal.append.latency": "histogram",
    "easycat.sessions.active": "observable_gauge",
    "easycat.turns.total": "counter",
    "easycat.audio.bytes.total": "counter",
    "easycat.audio.frames.total": "counter",
    "easycat.provider.errors.total": "counter",
    "easycat.session.errors.total": "counter",
    "easycat.transport.disconnects.total": "counter",
    "easycat.validation.failures.total": "counter",
    "easycat.queue.depth": "observable_gauge",
    "easycat.queue.dropped.total": "counter",
    "easycat.event_loop.lag": "histogram",
    "easycat.journal.degraded": "observable_gauge",
}

LOW_CARDINALITY_ATTRIBUTE_KEYS = frozenset(
    {
        "easycat.provider",
        "easycat.provider_family",
        "easycat.surface",
        "easycat.transport",
        "easycat.debug_mode",
        "easycat.stage",
        "easycat.condition_id",
        "easycat.feature_set",
        "easycat.result",
        "easycat.error_type",
    }
)

SPAN_ATTRIBUTE_KEYS = LOW_CARDINALITY_ATTRIBUTE_KEYS | frozenset(
    {
        "gen_ai.operation.name",
        "gen_ai.request.model",
        "gen_ai.system",
    }
)
ALLOWED_ATTRIBUTE_KEYS = LOW_CARDINALITY_ATTRIBUTE_KEYS

FORBIDDEN_ATTRIBUTE_KEYS = frozenset(
    {
        "api_key",
        "file_path",
        "generated_text",
        "ip_address",
        "model_output",
        "phone_number",
        "prompt",
        "provider_request_id",
        "session_id",
        "span_id",
        "tool_arguments",
        "trace_id",
        "transcript",
        "turn_id",
        "url",
        "user_id",
    }
)

# Defense-in-depth against new PII-bearing keys: any attribute whose name
# *contains* one of these substrings is rejected even when it is not on the
# explicit forbidden list above.  The explicit allow-list is consulted FIRST
# (see :func:`sanitize_attributes`) so currently-allowed keys are never caught
# — none of the allowed ``easycat.*`` / ``gen_ai.*`` keys contain a substring.
_FORBIDDEN_SUBSTRINGS = (
    "transcript",
    "prompt",
    "content",
    "text",
    "body",
    "secret",
    "token",
)

_COUNTERS: dict[str, Any] = {}
_HISTOGRAMS: dict[str, Any] = {}
_GAUGES: dict[str, Any] = {}
_GAUGE_VALUES: dict[
    str,
    dict[tuple[tuple[str, str | int | float | bool], ...], tuple[int | float, dict[str, Any]]],
] = {}
_GAUGE_LOCK = threading.Lock()
_ACTIVE_SESSIONS = 0
_ACTIVE_SESSIONS_LOCK = threading.Lock()


@contextlib.contextmanager
def span(name: str, attributes: Mapping[str, Any] | None = None) -> Iterator[None]:
    """Start an OTel span when tracing is configured; otherwise no-op."""
    if name not in SPAN_NAMES:
        raise ValueError(f"unknown EasyCat span name: {name}")
    safe_attributes = sanitize_attributes(attributes, allowed_keys=SPAN_ATTRIBUTE_KEYS)
    tracer = _get_tracer()
    if tracer is None:
        yield
        return
    with tracer.start_as_current_span(name, attributes=safe_attributes):
        yield


def record_histogram(
    name: str,
    value: float,
    attributes: Mapping[str, Any] | None = None,
) -> None:
    _record_metric(name, "histogram", value, attributes)


def increment_counter(
    name: str,
    value: int | float = 1,
    attributes: Mapping[str, Any] | None = None,
) -> None:
    _record_metric(name, "counter", value, attributes)


def observe_gauge(
    name: str,
    value: int | float,
    attributes: Mapping[str, Any] | None = None,
) -> None:
    _record_metric(name, "observable_gauge", value, attributes)


def session_started() -> None:
    global _ACTIVE_SESSIONS
    with _ACTIVE_SESSIONS_LOCK:
        _ACTIVE_SESSIONS += 1
        active = _ACTIVE_SESSIONS
    observe_gauge("easycat.sessions.active", active)


def session_ended() -> None:
    global _ACTIVE_SESSIONS
    with _ACTIVE_SESSIONS_LOCK:
        _ACTIVE_SESSIONS = max(0, _ACTIVE_SESSIONS - 1)
        active = _ACTIVE_SESSIONS
    observe_gauge("easycat.sessions.active", active)


def sanitize_attributes(
    attributes: Mapping[str, Any] | None = None,
    *,
    allowed_keys: frozenset[str] = ALLOWED_ATTRIBUTE_KEYS,
) -> dict[str, Any]:
    if not attributes:
        return {}

    sanitized: dict[str, Any] = {}
    for key, value in attributes.items():
        normalized = str(key)
        # Check explicit membership first so an allowed key is never falsely
        # rejected by the substring guard below.
        if normalized in allowed_keys:
            sanitized[normalized] = _safe_attribute_value(value)
            continue
        low = normalized.lower()
        if normalized in FORBIDDEN_ATTRIBUTE_KEYS or any(
            substring in low for substring in _FORBIDDEN_SUBSTRINGS
        ):
            raise ValueError(f"forbidden observability attribute: {normalized}")
        raise ValueError(f"unsupported observability attribute: {normalized}")
    return sanitized


def _record_metric(
    name: str,
    expected_kind: MetricKind,
    value: int | float,
    attributes: Mapping[str, Any] | None,
) -> None:
    if METRIC_DEFINITIONS.get(name) != expected_kind:
        raise ValueError(f"metric {name!r} is not a {expected_kind}")
    safe_attributes = sanitize_attributes(attributes)
    if expected_kind == "observable_gauge":
        _update_gauge_value(name, value, safe_attributes)
        meter = _get_meter()
        if meter is None:
            return
        instrument = _GAUGES.get(name)
        if instrument is None and hasattr(meter, "create_observable_gauge"):
            instrument = _create_observable_gauge(meter, name)
            _GAUGES[name] = instrument
        # Compatibility for test fakes or alternate metric APIs. Real OTel
        # observable gauges read values from the registered callback above.
        if hasattr(instrument, "observe"):
            instrument.observe(value, attributes=safe_attributes)
        return

    meter = _get_meter()
    if meter is None:
        return
    if expected_kind == "counter":
        instrument = _COUNTERS.get(name)
        if instrument is None:
            instrument = meter.create_counter(name)
            _COUNTERS[name] = instrument
        instrument.add(value, attributes=safe_attributes)
        return
    if expected_kind == "histogram":
        instrument = _HISTOGRAMS.get(name)
        if instrument is None:
            instrument = meter.create_histogram(name)
            _HISTOGRAMS[name] = instrument
        instrument.record(value, attributes=safe_attributes)
        return


def _safe_attribute_value(value: Any) -> str | int | float | bool:
    if isinstance(value, bool | int | float):
        return value
    return str(value)


def _update_gauge_value(name: str, value: int | float, attributes: dict[str, Any]) -> None:
    key = tuple(sorted(attributes.items()))
    with _GAUGE_LOCK:
        values = _GAUGE_VALUES.setdefault(name, {})
        values[key] = (value, attributes)


def _create_observable_gauge(meter: Any, name: str) -> Any:
    callback = _gauge_callback(name)
    try:
        return meter.create_observable_gauge(name, callbacks=[callback])
    except TypeError:
        return meter.create_observable_gauge(name)


def _gauge_callback(name: str) -> Any:
    def _callback(_options: Any = None) -> list[Any]:
        with _GAUGE_LOCK:
            observations = list(_GAUGE_VALUES.get(name, {}).values())
        return [_make_observation(value, attributes) for value, attributes in observations]

    return _callback


def _make_observation(value: int | float, attributes: dict[str, Any]) -> Any:
    from opentelemetry.metrics import Observation

    return Observation(value, attributes=attributes)


def _get_tracer() -> Any | None:
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    return trace.get_tracer(INSTRUMENTATION_NAME)


def _get_meter() -> Any | None:
    try:
        from opentelemetry import metrics
    except ImportError:
        return None
    return metrics.get_meter(INSTRUMENTATION_NAME)
