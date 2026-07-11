"""Public latency-validation domain API.

Implementation modules remain private so orchestration code can depend on
focused leaves while callers retain one explicit import surface.
"""

from easycat.validation._failure_classification import (
    FailureCategory,
    classify_failure_category,
    classify_latency_failure,
)
from easycat.validation._latency_artifacts import (
    append_reliability_sample,
    build_latency_artifact,
    build_reliability_artifact,
    load_latency_samples,
    load_reliability_samples,
)
from easycat.validation._latency_baseline import compare_latency_baseline
from easycat.validation._latency_budgets import (
    DEFAULT_BUDGETS,
    DEFAULT_RELIABILITY_BUDGETS,
    LatencyBudget,
    LatencyBudgetViolation,
    ReliabilityBudget,
    ReliabilityBudgetViolation,
    evaluate_budgets,
    evaluate_reliability_budgets,
)
from easycat.validation._latency_models import (
    LatencyComparisonThresholds,
    LatencyMode,
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
    latency_pytest_args,
)

__all__ = [
    "DEFAULT_BUDGETS",
    "DEFAULT_RELIABILITY_BUDGETS",
    "FailureCategory",
    "LATENCY_SMOKE_TEST",
    "LATENCY_SWEEP_TEST",
    "LATENCY_TEST_FILE",
    "LatencyBudget",
    "LatencyBudgetViolation",
    "LatencyComparisonThresholds",
    "LatencyMode",
    "LatencyPercentileStats",
    "LatencySample",
    "LatencyStageDurations",
    "ReliabilityBudget",
    "ReliabilityBudgetViolation",
    "ReliabilitySample",
    "ReliabilitySignals",
    "append_reliability_sample",
    "build_latency_artifact",
    "build_reliability_artifact",
    "classify_failure_category",
    "classify_latency_failure",
    "compare_latency_baseline",
    "evaluate_budgets",
    "evaluate_reliability_budgets",
    "latency_pytest_args",
    "load_latency_samples",
    "load_reliability_samples",
]
