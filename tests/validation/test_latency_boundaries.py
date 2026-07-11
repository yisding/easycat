"""Architecture contracts for the latency validation domain."""

from __future__ import annotations

import pytest

import easycat.validation.latency as latency
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


def test_public_latency_facade_reexports_leaf_implementations() -> None:
    assert latency.LatencySample is LatencySample
    assert latency.LatencyBudget is LatencyBudget
    assert latency.ReliabilitySample is ReliabilitySample
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
