"""Latency and reliability budget policy."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any

from easycat._numeric import is_finite_number
from easycat.validation._latency_models import (
    LatencyStageDurations,
    ReliabilitySample,
    ReliabilitySignals,
)

__all__ = [
    "DEFAULT_BUDGETS",
    "DEFAULT_RELIABILITY_BUDGETS",
    "LatencyBudget",
    "LatencyBudgetViolation",
    "ReliabilityBudget",
    "ReliabilityBudgetViolation",
    "evaluate_budgets",
    "evaluate_reliability_budgets",
]

_LATENCY_BUDGET_STAGES = frozenset(item.name for item in fields(LatencyStageDurations))
_RELIABILITY_BUDGET_SIGNALS = frozenset(
    item.name for item in fields(ReliabilitySignals) if item.name != "unavailable_reason"
)


@dataclass(frozen=True, slots=True)
class LatencyBudget:
    stage: str
    max_ms: float
    percentile: str = "p95"

    def __post_init__(self) -> None:
        if self.stage not in _LATENCY_BUDGET_STAGES:
            raise ValueError(
                "LatencyBudget stage must be one of "
                f"{', '.join(sorted(_LATENCY_BUDGET_STAGES))}; got {self.stage!r}"
            )
        if not is_finite_number(self.max_ms) or self.max_ms < 0:
            raise ValueError("LatencyBudget max_ms must be a finite number >= 0")
        if self.percentile not in ("p50", "p90", "p95", "p99"):
            raise ValueError(
                f"LatencyBudget percentile must be one of p50, p90, p95, p99; "
                f"got {self.percentile!r}"
            )

    def to_dict(self) -> dict[str, float | str]:
        return {"stage": self.stage, "max_ms": self.max_ms, "percentile": self.percentile}


# Calibrated against the live-stack SLO defaults in
# tests/e2e/test_plan_7_latency_benchmark.py. These tolerate provider jitter
# while still catching order-of-magnitude regressions.
DEFAULT_BUDGETS: tuple[LatencyBudget, ...] = (
    LatencyBudget(stage="total_ms", max_ms=8000.0, percentile="p95"),
    LatencyBudget(stage="tts_ttfb_ms", max_ms=1500.0, percentile="p95"),
    LatencyBudget(stage="llm_ttft_ms", max_ms=2500.0, percentile="p95"),
    LatencyBudget(stage="interruption_cutoff_ms", max_ms=400.0, percentile="p95"),
)


@dataclass(frozen=True, slots=True)
class LatencyBudgetViolation:
    stage: str
    percentile: str
    observed_ms: float
    budget_ms: float
    scope: str

    def to_dict(self) -> dict[str, float | str]:
        return {
            "stage": self.stage,
            "percentile": self.percentile,
            "observed_ms": self.observed_ms,
            "budget_ms": self.budget_ms,
            "scope": self.scope,
        }


def evaluate_budgets(
    percentiles: Mapping[str, Any],
    budgets: Sequence[LatencyBudget],
) -> list[LatencyBudgetViolation]:
    violations: list[LatencyBudgetViolation] = []
    if "overall" in percentiles:
        overall = percentiles["overall"]
        if not isinstance(overall, Mapping):
            raise ValueError("latency percentiles overall must be an object")
        violations.extend(_evaluate_scope(overall, budgets, scope="overall"))
    if "by_condition" in percentiles:
        by_condition = percentiles["by_condition"]
        if not isinstance(by_condition, Mapping):
            raise ValueError("latency percentiles by_condition must be an object")
        for condition_id, stage_stats in sorted(
            by_condition.items(), key=lambda item: str(item[0])
        ):
            if not isinstance(stage_stats, Mapping):
                raise ValueError(  # noqa: TRY004 domain-specific validation error
                    f"latency percentile condition {condition_id!r} must be an object"
                )
            violations.extend(
                _evaluate_scope(stage_stats, budgets, scope=f"condition:{condition_id}")
            )
    return violations


def _evaluate_scope(
    stage_stats: Mapping[str, Any],
    budgets: Sequence[LatencyBudget],
    *,
    scope: str,
) -> list[LatencyBudgetViolation]:
    results: list[LatencyBudgetViolation] = []
    for budget in budgets:
        if budget.stage not in stage_stats:
            continue
        stats = stage_stats[budget.stage]
        if not isinstance(stats, Mapping):
            raise ValueError(  # noqa: TRY004 domain-specific validation error
                "latency percentile stats must be an object; "
                f"got {type(stats).__name__} for {scope}:{budget.stage}"
            )
        observed = stats.get(budget.percentile)
        if observed is None:
            continue
        if not is_finite_number(observed) or observed < 0:
            raise ValueError(
                "observed latency percentile must be a finite number >= 0; "
                f"got {observed!r} for {scope}:{budget.stage}.{budget.percentile}"
            )
        observed_ms = float(observed)
        if observed_ms > budget.max_ms:
            results.append(
                LatencyBudgetViolation(
                    stage=budget.stage,
                    percentile=budget.percentile,
                    observed_ms=observed_ms,
                    budget_ms=float(budget.max_ms),
                    scope=scope,
                )
            )
    return results


@dataclass(frozen=True, slots=True)
class ReliabilityBudget:
    """Maximum accepted value for one reliability signal."""

    signal: str
    max_value: float

    def __post_init__(self) -> None:
        if self.signal not in _RELIABILITY_BUDGET_SIGNALS:
            raise ValueError(
                "ReliabilityBudget signal must be one of "
                f"{', '.join(sorted(_RELIABILITY_BUDGET_SIGNALS))}; got {self.signal!r}"
            )
        if not is_finite_number(self.max_value) or self.max_value < 0:
            raise ValueError("ReliabilityBudget max_value must be a finite number >= 0")

    def to_dict(self) -> dict[str, float | str]:
        return {"signal": self.signal, "max_value": self.max_value}


DEFAULT_RELIABILITY_BUDGETS: tuple[ReliabilityBudget, ...] = (
    ReliabilityBudget(signal="event_loop_lag_ms", max_value=250.0),
    ReliabilityBudget(signal="memory_growth_kib", max_value=512_000.0),
    ReliabilityBudget(signal="dropped_frames", max_value=0.0),
    ReliabilityBudget(signal="journal_degraded", max_value=0.0),
)


@dataclass(frozen=True, slots=True)
class ReliabilityBudgetViolation:
    signal: str
    observed: float
    budget: float
    scope: str

    def to_dict(self) -> dict[str, float | str]:
        return {
            "signal": self.signal,
            "observed": self.observed,
            "budget": self.budget,
            "scope": self.scope,
        }


def evaluate_reliability_budgets(
    samples: Sequence[ReliabilitySample],
    budgets: Sequence[ReliabilityBudget],
) -> list[ReliabilityBudgetViolation]:
    """Evaluate eligible samples overall and for each condition."""
    eligible = [sample for sample in samples if sample.eligible]
    if not eligible:
        return []
    by_condition: dict[str, list[ReliabilitySample]] = defaultdict(list)
    for sample in eligible:
        by_condition[sample.condition_id].append(sample)
    violations = _evaluate_reliability_scope(eligible, budgets, scope="overall")
    for condition_id, condition_samples in sorted(by_condition.items()):
        violations.extend(
            _evaluate_reliability_scope(
                condition_samples, budgets, scope=f"condition:{condition_id}"
            )
        )
    return violations


def _evaluate_reliability_scope(
    samples: Sequence[ReliabilitySample],
    budgets: Sequence[ReliabilityBudget],
    *,
    scope: str,
) -> list[ReliabilityBudgetViolation]:
    results: list[ReliabilityBudgetViolation] = []
    for budget in budgets:
        observed_values = [
            value
            for sample in samples
            if (value := _reliability_signal_value(sample, budget.signal)) is not None
        ]
        if not observed_values:
            continue
        observed = max(observed_values)
        if observed > budget.max_value:
            results.append(
                ReliabilityBudgetViolation(
                    signal=budget.signal,
                    observed=observed,
                    budget=float(budget.max_value),
                    scope=scope,
                )
            )
    return results


def _reliability_signal_value(sample: ReliabilitySample, signal: str) -> float | None:
    value = getattr(sample.signals, signal, None)
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    return float(value)
