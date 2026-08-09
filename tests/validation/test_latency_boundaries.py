"""Architecture contracts for the latency validation domain."""

from __future__ import annotations

import json

import pytest

from easycat.validation import latency
from easycat.validation._latency_artifacts import (
    load_latency_samples,
    load_reliability_samples,
)
from easycat.validation._latency_budgets import (
    LatencyBudget,
    LatencyBudgetViolation,
    ReliabilityBudget,
    ReliabilityBudgetViolation,
)
from easycat.validation._latency_models import (
    LatencyComparisonThresholds,
    LatencyPercentileStats,
    LatencySample,
    LatencyStageDurations,
    ReliabilitySample,
    ReliabilitySignals,
)
from easycat.validation._latency_selectors import (
    LATENCY_SMOKE_TEST,
    LATENCY_SWEEP_TEST,
    LATENCY_TEST_FILE,
)


def test_public_latency_facade_reexports_leaf_implementations() -> None:
    assert latency.LatencySample is LatencySample
    assert latency.LatencyBudget is LatencyBudget
    assert latency.ReliabilitySample is ReliabilitySample
    assert latency.LATENCY_TEST_FILE is LATENCY_TEST_FILE
    assert latency.LATENCY_SMOKE_TEST is LATENCY_SMOKE_TEST
    assert latency.LATENCY_SWEEP_TEST is LATENCY_SWEEP_TEST
    assert set(latency.__all__) == {name for name in vars(latency) if not name.startswith("_")}


@pytest.mark.parametrize(
    "model",
    [
        LatencyComparisonThresholds,
        LatencyPercentileStats,
        LatencyBudget,
        LatencyBudgetViolation,
        LatencyStageDurations,
        LatencySample,
        ReliabilityBudget,
        ReliabilityBudgetViolation,
        ReliabilitySignals,
        ReliabilitySample,
    ],
)
def test_latency_value_objects_are_slotted(model: type[object]) -> None:
    assert "__slots__" in vars(model)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -1.0, True])
def test_latency_budget_rejects_invalid_maximum(value: object) -> None:
    with pytest.raises(ValueError, match="max_ms"):
        LatencyBudget(stage="total_ms", max_ms=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -1.0, True])
def test_reliability_budget_rejects_invalid_maximum(value: object) -> None:
    with pytest.raises(ValueError, match="max_value"):
        ReliabilityBudget(signal="queue_depth", max_value=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("stage", ["totl_ms", "", True])
def test_latency_budget_rejects_unknown_stage(stage: object) -> None:
    with pytest.raises(ValueError, match="LatencyBudget stage"):
        LatencyBudget(stage=stage, max_ms=100.0)  # type: ignore[arg-type]


@pytest.mark.parametrize("signal", ["queue_dept", "unavailable_reason", True])
def test_reliability_budget_rejects_unknown_or_non_numeric_signal(signal: object) -> None:
    with pytest.raises(ValueError, match="ReliabilityBudget signal"):
        ReliabilityBudget(signal=signal, max_value=1.0)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("relative_regression", float("nan")),
        ("relative_regression", float("inf")),
        ("relative_regression", -0.1),
        ("relative_regression", True),
        ("absolute_regression_ms", float("nan")),
        ("absolute_regression_ms", float("inf")),
        ("absolute_regression_ms", -1.0),
        ("absolute_regression_ms", True),
    ],
)
def test_comparison_thresholds_reject_invalid_numeric_values(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        LatencyComparisonThresholds(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "3"])
def test_comparison_thresholds_require_positive_integer_sample_count(value: object) -> None:
    with pytest.raises(ValueError, match="min_samples"):
        LatencyComparisonThresholds(min_samples=value)  # type: ignore[arg-type]


def test_comparison_thresholds_reject_unknown_percentile_at_construction() -> None:
    with pytest.raises(ValueError, match="p42"):
        LatencyComparisonThresholds(regression_percentile="p42")  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["not-false", 1, 0, [], {}])
def test_sample_boolean_fields_reject_ambiguous_values(value: object) -> None:
    with pytest.raises(ValueError, match="boolean values"):
        LatencySample.from_dict(
            {
                "sample_id": "invalid-bool",
                "condition_id": "baseline",
                "warmup": value,
                "stages": {},
            }
        )


@pytest.mark.parametrize(
    ("loader", "kind"),
    [
        (load_latency_samples, "latency"),
        (load_reliability_samples, "reliability"),
    ],
)
def test_sample_loaders_reject_non_object_entries(loader: object, kind: str) -> None:
    with pytest.raises(ValueError, match=rf"{kind} sample at index 1 must be an object"):
        loader('[{"sample_id":"one","condition_id":"baseline"},null]')  # type: ignore[operator]


@pytest.mark.parametrize("stages", [None, [], "corrupt", True])
def test_latency_sample_loader_rejects_present_non_object_stages(stages: object) -> None:
    raw = json.dumps([{"sample_id": "one", "condition_id": "baseline", "stages": stages}])

    with pytest.raises(ValueError, match="latency sample stages must be an object"):
        load_latency_samples(raw)


@pytest.mark.parametrize("signals", [None, [], "corrupt", True])
def test_reliability_sample_loader_rejects_present_non_object_signals(
    signals: object,
) -> None:
    raw = json.dumps([{"sample_id": "one", "condition_id": "baseline", "signals": signals}])

    with pytest.raises(ValueError, match="reliability sample signals must be an object"):
        load_reliability_samples(raw)


@pytest.mark.parametrize("value", [True, -1.0, float("nan"), float("inf")])
def test_latency_percentiles_reject_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="latency percentile value"):
        LatencyPercentileStats.from_values([value])  # type: ignore[list-item]


@pytest.mark.parametrize("value", [True, -1.0, float("nan"), float("inf")])
def test_latency_stages_reject_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="total_ms"):
        LatencyStageDurations(total_ms=value)  # type: ignore[arg-type]


def test_latency_sample_parser_rejects_boolean_stage_value() -> None:
    with pytest.raises(ValueError, match="finite number"):
        LatencySample.from_dict(
            {
                "sample_id": "invalid-stage",
                "condition_id": "baseline",
                "stages": {"total_ms": True},
            }
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("sample_id", 1),
        ("condition_id", []),
        ("timestamp_source", {}),
        ("missing_stage_reason", []),
        ("failure_class", {}),
    ],
)
def test_latency_sample_parser_rejects_non_string_scalar_fields(
    field_name: str,
    value: object,
) -> None:
    payload: dict[str, object] = {
        "sample_id": "sample",
        "condition_id": "baseline",
        "stages": {},
        field_name: value,
    }

    with pytest.raises(ValueError, match=rf"{field_name} must be a string"):
        LatencySample.from_dict(payload)


@pytest.mark.parametrize("field_name", ["provider", "model", "transport", "debug"])
def test_latency_sample_parser_rejects_non_object_metadata(field_name: str) -> None:
    with pytest.raises(ValueError, match=rf"{field_name} must be an object"):
        LatencySample.from_dict(
            {
                "sample_id": "sample",
                "condition_id": "baseline",
                "stages": {},
                field_name: "corrupt",
            }
        )


@pytest.mark.parametrize(
    "metadata",
    [{"provider": 1}, {1: "provider"}],
)
def test_latency_sample_parser_rejects_non_string_metadata_entries(
    metadata: dict[object, object],
) -> None:
    with pytest.raises(ValueError, match="provider entries must be strings"):
        LatencySample.from_dict(
            {
                "sample_id": "sample",
                "condition_id": "baseline",
                "stages": {},
                "provider": metadata,
            }
        )


@pytest.mark.parametrize(
    ("signal", "value"),
    [
        ("event_loop_lag_ms", True),
        ("event_loop_lag_ms", -1.0),
        ("queue_depth", -1),
        ("dropped_frames", float("inf")),
        ("active_sessions", True),
        ("memory_growth_kib", 10**1000),
    ],
)
def test_reliability_signals_reject_invalid_numeric_values(signal: str, value: object) -> None:
    with pytest.raises(ValueError, match=signal):
        ReliabilitySignals(**{signal: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [10**1000, str(10**1000)])
def test_reliability_sample_parser_rejects_float_unrepresentable_integers(value: object) -> None:
    with pytest.raises(ValueError, match="finite integers"):
        ReliabilitySample.from_dict(
            {
                "sample_id": "invalid-integer",
                "condition_id": "baseline",
                "signals": {"queue_depth": value},
            }
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("sample_id", 1),
        ("condition_id", []),
        ("mode", {}),
    ],
)
def test_reliability_sample_parser_rejects_non_string_scalar_fields(
    field_name: str,
    value: object,
) -> None:
    payload: dict[str, object] = {
        "sample_id": "sample",
        "condition_id": "baseline",
        "signals": {},
        field_name: value,
    }

    with pytest.raises(ValueError, match=rf"{field_name} must be a string"):
        ReliabilitySample.from_dict(payload)


def test_reliability_sample_parser_rejects_non_string_unavailable_reason() -> None:
    with pytest.raises(ValueError, match="unavailable_reason must be a string"):
        ReliabilitySample.from_dict(
            {
                "sample_id": "sample",
                "condition_id": "baseline",
                "signals": {"unavailable_reason": {}},
            }
        )
