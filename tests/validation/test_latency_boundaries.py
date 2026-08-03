"""Architecture contracts for the latency validation domain."""

from __future__ import annotations

import pytest

from easycat.validation import latency
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
