from __future__ import annotations

import json
import os
import platform
import statistics
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from statistics import median
from typing import Any, Literal

LATENCY_TEST_FILE = "tests/e2e/test_plan_7_latency_benchmark.py"
LATENCY_SMOKE_TEST = "test_single_full_stack_latency_probe"
LATENCY_SWEEP_TEST = "test_latency_benchmark_by_pipeline_flags"


class LatencyMode(StrEnum):
    SMOKE = "smoke"
    SWEEP = "sweep"


@dataclass(frozen=True)
class LatencyComparisonThresholds:
    relative_regression: float = 0.2
    absolute_regression_ms: float = 200.0
    min_samples: int = 3
    regression_percentile: Literal["p50", "p90", "p95", "p99"] = "p95"

    def to_dict(self) -> dict[str, float | int | str]:
        return {
            "relative_regression": self.relative_regression,
            "absolute_regression_ms": self.absolute_regression_ms,
            "min_samples": self.min_samples,
            "regression_percentile": self.regression_percentile,
        }


@dataclass(frozen=True)
class LatencyPercentileStats:
    count: int
    p50: float | None
    p90: float | None
    p95: float | None
    p99: float | None

    def to_dict(self) -> dict[str, float | int | None]:
        return {
            "count": self.count,
            "p50": self.p50,
            "p90": self.p90,
            "p95": self.p95,
            "p99": self.p99,
        }

    @classmethod
    def from_values(cls, values: Sequence[float | None]) -> LatencyPercentileStats:
        cleaned = [float(value) for value in values if value is not None]
        count = len(cleaned)
        if count == 0:
            return cls(count=0, p50=None, p90=None, p95=None, p99=None)
        if count == 1:
            only = cleaned[0]
            return cls(count=1, p50=only, p90=only, p95=only, p99=only)
        # exclusive: (N+1)*p formula matches operator-intuition for tail samples
        cuts = statistics.quantiles(cleaned, n=100, method="exclusive")
        return cls(
            count=count,
            p50=cuts[49],
            p90=cuts[89],
            p95=cuts[94],
            p99=cuts[98],
        )


@dataclass(frozen=True)
class LatencyBudget:
    stage: str
    max_ms: float
    percentile: str = "p95"

    def __post_init__(self) -> None:
        if self.percentile not in ("p50", "p90", "p95", "p99"):
            raise ValueError(
                f"LatencyBudget percentile must be one of p50, p90, p95, p99; "
                f"got {self.percentile!r}"
            )

    def to_dict(self) -> dict[str, float | str]:
        return {"stage": self.stage, "max_ms": self.max_ms, "percentile": self.percentile}


# Calibrated against the live-stack SLO defaults in
# tests/e2e/test_plan_7_latency_benchmark.py (baseline p50 5000 ms, p90 6500 ms,
# per-probe sanity bound 8000 ms). Loose enough to ride out live-API jitter,
# tight enough that an order-of-magnitude regression still fails CI.
DEFAULT_BUDGETS: tuple[LatencyBudget, ...] = (
    LatencyBudget(stage="total_ms", max_ms=8000.0, percentile="p95"),
    LatencyBudget(stage="tts_ttfb_ms", max_ms=1500.0, percentile="p95"),
    LatencyBudget(stage="llm_ttft_ms", max_ms=2500.0, percentile="p95"),
)


@dataclass(frozen=True)
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
    overall = percentiles.get("overall")
    if isinstance(overall, Mapping):
        violations.extend(_evaluate_scope(overall, budgets, scope="overall"))
    by_condition = percentiles.get("by_condition")
    if isinstance(by_condition, Mapping):
        for condition_id, stage_stats in sorted(
            by_condition.items(), key=lambda item: str(item[0])
        ):
            if not isinstance(stage_stats, Mapping):
                continue
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
        stats = stage_stats.get(budget.stage)
        if not isinstance(stats, Mapping):
            continue
        observed = stats.get(budget.percentile)
        if observed is None:
            continue
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


@dataclass(frozen=True)
class ReliabilityBudget:
    """A pass/fail threshold for a single reliability signal.

    ``observed`` is the maximum value of ``signal`` across all eligible
    reliability samples (boolean signals such as ``journal_degraded`` are
    coerced to ``0``/``1``). A budget is violated when the observed maximum
    exceeds ``max_value``; use ``max_value=0`` to require the signal to never
    fire (e.g. ``dropped_frames``, ``journal_degraded``).
    """

    signal: str
    max_value: float

    def to_dict(self) -> dict[str, float | str]:
        return {"signal": self.signal, "max_value": self.max_value}


# Reliability budgets are evaluated over *eligible* samples only (SMOKE runs
# mark their samples informational, so they never gate). Thresholds are loose
# enough to ride out normal CI jitter but tight enough that a saturated event
# loop, a leak, or any dropped audio still fails the run.
DEFAULT_RELIABILITY_BUDGETS: tuple[ReliabilityBudget, ...] = (
    ReliabilityBudget(signal="event_loop_lag_ms", max_value=250.0),
    ReliabilityBudget(signal="memory_growth_kib", max_value=512_000.0),
    ReliabilityBudget(signal="dropped_frames", max_value=0.0),
    ReliabilityBudget(signal="journal_degraded", max_value=0.0),
)


@dataclass(frozen=True)
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


def _reliability_signal_value(sample: ReliabilitySample, signal: str) -> float | None:
    value = getattr(sample.signals, signal, None)
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    return float(value)


def evaluate_reliability_budgets(
    samples: Sequence[ReliabilitySample],
    budgets: Sequence[ReliabilityBudget],
) -> list[ReliabilityBudgetViolation]:
    """Compare eligible reliability samples against their budgets.

    Only ``eligible`` samples participate (SMOKE/informational samples are
    skipped). For each budget the maximum observed value across eligible
    samples is compared to ``max_value``; the result is grouped both overall
    and per condition so consumers can localize the offending condition.
    """
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


def latency_pytest_args(mode: LatencyMode | str) -> list[str]:
    mode = LatencyMode(mode)
    if mode is LatencyMode.SMOKE:
        return [f"{LATENCY_TEST_FILE}::{LATENCY_SMOKE_TEST}"]
    return [f"{LATENCY_TEST_FILE}::{LATENCY_SWEEP_TEST}"]


@dataclass(frozen=True)
class LatencyStageDurations:
    detection_ms: float | None = None
    stt_ms: float | None = None
    stt_finalize_close_ms: float | None = None
    agent_request_start_ms: float | None = None
    llm_ttft_ms: float | None = None
    tts_ttfb_ms: float | None = None
    transport_ms: float | None = None
    total_ms: float | None = None

    def to_dict(self) -> dict[str, float | None]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass(frozen=True)
class LatencySample:
    sample_id: str
    condition_id: str
    warmup: bool
    timestamp_source: str
    stages: LatencyStageDurations
    provider: dict[str, str] | None = None
    model: dict[str, str] | None = None
    transport: dict[str, str] | None = None
    debug: dict[str, str] | None = None
    missing_stage_reason: str | None = None
    failure_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "condition_id": self.condition_id,
            "warmup": self.warmup,
            "timestamp_source": self.timestamp_source,
            "provider": self.provider or {},
            "model": self.model or {},
            "transport": self.transport or {},
            "debug": self.debug or {},
            "stages": self.stages.to_dict(),
            "missing_stage_reason": self.missing_stage_reason,
            "failure_class": self.failure_class,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LatencySample:
        stages = payload.get("stages") or {}
        if not isinstance(stages, dict):
            stages = {}
        return cls(
            sample_id=str(payload["sample_id"]),
            condition_id=str(payload["condition_id"]),
            warmup=bool(payload.get("warmup", False)),
            timestamp_source=str(payload.get("timestamp_source", "unknown")),
            provider=_string_dict(payload.get("provider")),
            model=_string_dict(payload.get("model")),
            transport=_string_dict(payload.get("transport")),
            debug=_string_dict(payload.get("debug")),
            stages=LatencyStageDurations(
                **{
                    item.name: _float_or_none(stages.get(item.name))
                    for item in fields(LatencyStageDurations)
                }
            ),
            missing_stage_reason=_optional_string(payload.get("missing_stage_reason")),
            failure_class=_optional_string(payload.get("failure_class")),
        )


@dataclass(frozen=True)
class ReliabilitySignals:
    event_loop_lag_ms: float | None = None
    queue_depth: int | None = None
    dropped_frames: int | None = None
    journal_degraded: bool | None = None
    active_sessions: int | None = None
    memory_growth_kib: int | None = None
    unavailable_reason: str | None = None

    def to_dict(self) -> dict[str, float | int | bool | str | None]:
        return {
            item.name: value
            for item in fields(self)
            if (value := getattr(self, item.name)) is not None
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ReliabilitySignals:
        return cls(
            event_loop_lag_ms=_float_or_none(payload.get("event_loop_lag_ms")),
            queue_depth=_int_or_none(payload.get("queue_depth")),
            dropped_frames=_int_or_none(payload.get("dropped_frames")),
            journal_degraded=_bool_or_none(payload.get("journal_degraded")),
            active_sessions=_int_or_none(payload.get("active_sessions")),
            memory_growth_kib=_int_or_none(payload.get("memory_growth_kib")),
            unavailable_reason=_optional_string(payload.get("unavailable_reason")),
        )


@dataclass(frozen=True)
class ReliabilitySample:
    sample_id: str
    condition_id: str
    mode: str
    informational: bool
    eligible: bool
    signals: ReliabilitySignals

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "condition_id": self.condition_id,
            "mode": self.mode,
            "informational": self.informational,
            "eligible": self.eligible,
            "signals": self.signals.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ReliabilitySample:
        signals = payload.get("signals") or {}
        if not isinstance(signals, dict):
            signals = {}
        return cls(
            sample_id=str(payload["sample_id"]),
            condition_id=str(payload["condition_id"]),
            mode=str(payload.get("mode", "unknown")),
            informational=bool(payload.get("informational", True)),
            eligible=bool(payload.get("eligible", False)),
            signals=ReliabilitySignals.from_dict(signals),
        )


def build_latency_artifact(
    *,
    mode: LatencyMode | str,
    samples: list[LatencySample],
    reliability_samples: list[ReliabilitySample] | None = None,
    generated_at: datetime | None = None,
    baseline: dict[str, Any] | None = None,
    environment: dict[str, Any] | None = None,
    clock_source: str = "time.monotonic",
    budgets: Sequence[LatencyBudget] | None = None,
) -> dict[str, Any]:
    mode = LatencyMode(mode)
    generated_at = generated_at or datetime.now(UTC)
    effective_budgets: Sequence[LatencyBudget] = DEFAULT_BUDGETS if budgets is None else budgets
    percentiles = _build_percentile_block(samples)
    # Budgets enforce tail-latency SLOs and are only meaningful when the run
    # produced enough samples for those tails to be statistically eligible.
    # SMOKE runs are explicitly low-sample, so one slow probe would otherwise
    # turn the default `easycat validate latency` invocation into a hard fail.
    # Skip budget evaluation in SMOKE; sweep runs continue to enforce.
    budget_violations = (
        [violation.to_dict() for violation in evaluate_budgets(percentiles, effective_budgets)]
        if mode is not LatencyMode.SMOKE
        else []
    )
    return {
        "schema_version": 1,
        "kind": "latency_validation",
        "mode": mode.value,
        "generated_at": generated_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "baseline": baseline or {"comparison": "not_configured"},
        "environment": environment or _latency_environment_metadata(),
        "clock_source": clock_source,
        "samples": [sample.to_dict() for sample in samples],
        "reliability_samples": [sample.to_dict() for sample in reliability_samples or []],
        "summary": _summarize_samples(samples),
        "percentiles": percentiles,
        "budget_violations": budget_violations,
    }


def build_reliability_artifact(
    *,
    samples: list[ReliabilitySample],
    generated_at: datetime | None = None,
    budgets: Sequence[ReliabilityBudget] | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(UTC)
    effective_budgets: Sequence[ReliabilityBudget] = (
        DEFAULT_RELIABILITY_BUDGETS if budgets is None else budgets
    )
    budget_violations = [
        violation.to_dict()
        for violation in evaluate_reliability_budgets(samples, effective_budgets)
    ]
    return {
        "schema_version": 1,
        "kind": "reliability_validation",
        "generated_at": generated_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "samples": [sample.to_dict() for sample in samples],
        "summary": _summarize_reliability_samples(samples),
        "budget_violations": budget_violations,
    }


def load_latency_samples(raw: str) -> list[LatencySample]:
    import json

    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("latency samples payload must be a list")
    return [LatencySample.from_dict(item) for item in payload if isinstance(item, dict)]


def load_reliability_samples(raw: str) -> list[ReliabilitySample]:
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("reliability samples payload must be a list")
    return [ReliabilitySample.from_dict(item) for item in payload if isinstance(item, dict)]


def append_reliability_sample(path: str | Path, sample: ReliabilitySample) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        try:
            payload = json.loads(destination.read_text())
        except json.JSONDecodeError:
            payload = []
    else:
        payload = []
    if not isinstance(payload, list):
        payload = []
    payload.append(sample.to_dict())
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _summarize_reliability_samples(samples: list[ReliabilitySample]) -> dict[str, Any]:
    grouped: dict[str, list[ReliabilitySample]] = defaultdict(list)
    for sample in samples:
        grouped[sample.condition_id].append(sample)
    return {
        condition_id: {
            "count": len(items),
            "eligible_count": sum(1 for item in items if item.eligible),
            "informational_count": sum(1 for item in items if item.informational),
        }
        for condition_id, items in sorted(grouped.items())
    }


def compare_latency_baseline(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
    *,
    thresholds: LatencyComparisonThresholds | None = None,
) -> dict[str, Any]:
    thresholds = thresholds or LatencyComparisonThresholds()
    if thresholds.regression_percentile not in ("p50", "p90", "p95", "p99"):
        raise ValueError(
            f"regression_percentile must be one of p50, p90, p95, p99; "
            f"got {thresholds.regression_percentile!r}"
        )
    current_groups = _comparison_samples_by_condition(current)
    baseline_groups = _comparison_samples_by_condition(baseline)
    condition_results = [
        _compare_condition(
            condition_id,
            samples,
            baseline_groups.get(condition_id),
            thresholds,
            baseline,
        )
        for condition_id, samples in sorted(current_groups.items())
    ]
    statuses = {item["status"] for item in condition_results}
    if "fail" in statuses:
        status = "fail"
    elif "drift" in statuses:
        status = "drift"
    elif statuses == {"info"}:
        status = "info"
    else:
        status = "pass"
    return {
        "schema_version": 1,
        "kind": "latency_baseline_comparison",
        "status": status,
        "thresholds": thresholds.to_dict(),
        "conditions": condition_results,
    }


_PERCENTILE_STAGE_FIELDS: tuple[str, ...] = tuple(
    item.name for item in fields(LatencyStageDurations)
)


def _build_percentile_block(samples: list[LatencySample]) -> dict[str, Any]:
    eligible = [sample for sample in samples if not sample.warmup and sample.failure_class is None]
    overall = {
        stage: LatencyPercentileStats.from_values(
            [getattr(sample.stages, stage) for sample in eligible]
        ).to_dict()
        for stage in _PERCENTILE_STAGE_FIELDS
    }
    by_condition_samples: dict[str, list[LatencySample]] = defaultdict(list)
    for sample in eligible:
        by_condition_samples[sample.condition_id].append(sample)
    by_condition = {
        condition_id: {
            stage: LatencyPercentileStats.from_values(
                [getattr(sample.stages, stage) for sample in condition_samples]
            ).to_dict()
            for stage in _PERCENTILE_STAGE_FIELDS
        }
        for condition_id, condition_samples in sorted(by_condition_samples.items())
    }
    return {"overall": overall, "by_condition": by_condition}


def _summarize_samples(samples: list[LatencySample]) -> dict[str, Any]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for sample in samples:
        if sample.warmup or sample.failure_class:
            continue
        if sample.stages.total_ms is not None:
            grouped[sample.condition_id].append(sample.stages.total_ms)

    return {
        condition_id: _summarize_totals(totals) for condition_id, totals in sorted(grouped.items())
    }


def _comparison_samples_by_condition(
    artifact: Mapping[str, Any],
) -> dict[str, list[LatencySample]]:
    grouped: dict[str, list[LatencySample]] = defaultdict(list)
    raw_samples = artifact.get("samples")
    if not isinstance(raw_samples, list):
        return grouped
    for item in raw_samples:
        if not isinstance(item, dict):
            continue
        sample = LatencySample.from_dict(item)
        if sample.warmup or sample.failure_class or sample.stages.total_ms is None:
            continue
        grouped[sample.condition_id].append(sample)
    return grouped


def _compare_condition(
    condition_id: str,
    current_samples: list[LatencySample],
    baseline_samples: list[LatencySample] | None,
    thresholds: LatencyComparisonThresholds,
    baseline_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    if not baseline_samples:
        return {
            "condition_id": condition_id,
            "status": "info",
            "reason": "baseline_missing",
            "refresh_required": True,
        }

    baseline_version = _baseline_version(baseline_artifact, condition_id)
    if baseline_version is None:
        return {
            "condition_id": condition_id,
            "status": "drift",
            "failure_class": "provider_api_drift",
            "reason": "baseline_version_missing",
            "refresh_required": True,
        }

    if _has_mixed_signatures(current_samples) or _has_mixed_signatures(baseline_samples):
        return {
            "condition_id": condition_id,
            "status": "drift",
            "failure_class": "provider_api_drift",
            "reason": "mixed_condition_signature",
            "baseline_version": baseline_version,
            "refresh_required": True,
        }

    current_signature = _condition_signature(current_samples)
    baseline_signature = _condition_signature(baseline_samples)
    if current_signature != baseline_signature:
        return {
            "condition_id": condition_id,
            "status": "drift",
            "failure_class": "provider_api_drift",
            "reason": "condition_mismatch",
            "baseline_version": baseline_version,
            "refresh_required": True,
            "signature": {
                "current": current_signature,
                "baseline": baseline_signature,
            },
        }

    current_totals = [sample.stages.total_ms for sample in current_samples]
    baseline_totals = [sample.stages.total_ms for sample in baseline_samples]
    current_values = [value for value in current_totals if value is not None]
    baseline_values = [value for value in baseline_totals if value is not None]
    percentile = thresholds.regression_percentile
    current_observed = getattr(LatencyPercentileStats.from_values(current_values), percentile)
    baseline_observed = getattr(LatencyPercentileStats.from_values(baseline_values), percentile)
    if current_observed is None or baseline_observed is None:
        return {
            "condition_id": condition_id,
            "baseline_version": baseline_version,
            "current_count": len(current_values),
            "baseline_count": len(baseline_values),
            "percentile": percentile,
            "status": "info",
            "reason": "no_samples",
            "refresh_required": False,
        }
    delta_ms = current_observed - baseline_observed
    relative_delta = delta_ms / baseline_observed if baseline_observed > 0 else None
    relative_regression = (
        relative_delta is not None and relative_delta >= thresholds.relative_regression
    )
    absolute_regression = delta_ms >= thresholds.absolute_regression_ms

    base_result = {
        "condition_id": condition_id,
        "baseline_version": baseline_version,
        "current_count": len(current_values),
        "baseline_count": len(baseline_values),
        "percentile": percentile,
        f"current_{percentile}_ms": current_observed,
        f"baseline_{percentile}_ms": baseline_observed,
        "delta_ms": delta_ms,
        "relative_delta": relative_delta,
        "regression": {
            "relative": relative_regression,
            "absolute": absolute_regression,
        },
        "refresh_required": False,
    }

    current_ineligible = len(current_values) < thresholds.min_samples
    baseline_ineligible = len(baseline_values) < thresholds.min_samples
    if current_ineligible or baseline_ineligible:
        return {
            **base_result,
            "status": "info",
            "reason": "ineligible_sample_count",
        }
    if relative_regression and absolute_regression:
        return {
            **base_result,
            "status": "fail",
            "failure_class": "easycat_latency_regression",
        }
    return {
        **base_result,
        "status": "pass",
    }


def _condition_signature(samples: list[LatencySample]) -> dict[str, dict[str, str]]:
    sample = samples[0]
    return {
        "provider": sample.provider or {},
        "model": sample.model or {},
        "transport": sample.transport or {},
        "debug": sample.debug or {},
    }


def _has_mixed_signatures(samples: list[LatencySample]) -> bool:
    signatures = {_signature_key(_condition_signature([sample])) for sample in samples}
    return len(signatures) > 1


def _signature_key(
    signature: Mapping[str, Mapping[str, str]],
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    return tuple(
        (section, tuple(sorted(values.items()))) for section, values in sorted(signature.items())
    )


def _baseline_version(artifact: Mapping[str, Any], condition_id: str) -> str | None:
    baseline = artifact.get("baseline")
    if not isinstance(baseline, dict):
        return None
    conditions = baseline.get("conditions")
    if not isinstance(conditions, dict):
        return None
    condition = conditions.get(condition_id)
    if not isinstance(condition, dict):
        return None
    version = condition.get("version")
    if not version:
        return None
    return f"{version}:{condition_id}"


def _summarize_totals(totals: list[float]) -> dict[str, Any]:
    # Percentiles are intentionally omitted here: the `percentiles` block
    # (built via `LatencyPercentileStats.from_values`) is the single source of
    # truth for p50/p90/p95/p99. Reporting nearest-rank percentiles here too
    # produced a second, divergent set of numbers in the same artifact.
    sorted_totals = sorted(totals)
    return {
        "count": len(sorted_totals),
        "median_ms": median(sorted_totals) if sorted_totals else None,
    }


class FailureCategory(StrEnum):
    """Canonical failure buckets shared by latency and live classification.

    Both ``classify_latency_failure`` and ``runner.classify_live_failure``
    derive their (path-specific) ``failure_class`` strings from this single
    enum so the two paths can never silently disagree on which error tokens
    map to which bucket. The path-specific string vocabularies are kept
    distinct for back-compat; downstream consumers can normalize via
    ``classify_failure_category``.
    """

    AUTH = "auth"
    QUOTA = "quota"
    TIMEOUT = "timeout"
    NETWORK = "network"
    DRIFT = "drift"
    REGRESSION = "regression"
    OTHER = "other"


# Single source of truth for error-token matching, ordered by precedence.
# Tokens are matched against a normalized message (lowercased, with "_" and
# "-" collapsed to spaces) so both word- and identifier-style errors match.
#
# Precedence is deliberate and pinned by tests for the cross-category messages
# that the two paths historically disagreed on:
#   * QUOTA before AUTH: a rate-limit/quota signal (e.g. "429 unauthorized") is
#     the actionable one (back off / wait), so it wins over a co-occurring auth
#     token. This matches the original `classify_live_failure` ordering.
#   * DRIFT before NETWORK: schema-drift detection is a core purpose of live
#     validation (it feeds `_capability_status` -> status 'provider_drift'), so
#     a drift signal must not be masked by an incidental network word (e.g.
#     "schema mismatch on connection close"). This restores the original
#     `classify_live_failure` ordering, which checked DRIFT before NETWORK.
_FAILURE_CATEGORY_TOKENS: tuple[tuple[FailureCategory, tuple[str, ...]], ...] = (
    (
        FailureCategory.QUOTA,
        ("rate limit", "ratelimit", "429", "quota", "too many requests"),
    ),
    (
        FailureCategory.AUTH,
        (
            "api key",
            "auth",
            "unauthorized",
            "forbidden",
            "permission denied",
            "401",
            "403",
        ),
    ),
    (FailureCategory.TIMEOUT, ("timeout", "timed out", "deadline")),
    (FailureCategory.DRIFT, ("schema", "unknown event", "drift")),
    (FailureCategory.NETWORK, ("dns", "network", "connection")),
    (FailureCategory.REGRESSION, ("assert", "failed", "traceback")),
)


def classify_failure_category(message: str) -> FailureCategory:
    """Map an error message to its canonical :class:`FailureCategory`."""
    normalized = message.lower().replace("_", " ").replace("-", " ")
    for category, tokens in _FAILURE_CATEGORY_TOKENS:
        if any(token in normalized for token in tokens):
            return category
    return FailureCategory.OTHER


# Latency-path vocabulary (preserved for back-compat). Timeouts are treated as
# provider issues here and the catch-all is a latency regression.
_LATENCY_FAILURE_CLASSES: dict[FailureCategory, str] = {
    FailureCategory.AUTH: "provider_auth",
    FailureCategory.QUOTA: "provider_rate_limit",
    FailureCategory.TIMEOUT: "provider_timeout",
    FailureCategory.NETWORK: "provider_timeout",
    FailureCategory.DRIFT: "easycat_latency_regression",
    FailureCategory.REGRESSION: "easycat_latency_regression",
    FailureCategory.OTHER: "easycat_latency_regression",
}


def classify_latency_failure(message: str) -> str:
    return _LATENCY_FAILURE_CLASSES[classify_failure_category(message)]


def _is_ci() -> bool:
    """Whether we appear to be running under CI.

    Shared by every validation environment-metadata builder so the
    slice/live report and the latency report tag the identical environment
    with the same ``ci`` value. Accepts the GitHub-Actions-style ``CI=true``
    plus the generic ``CI=1``/``CI=yes`` set used by other providers.
    """
    return os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}


def _latency_environment_metadata() -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "ci": _is_ci(),
        "env_vars": {
            "OPENAI_API_KEY": bool(os.environ.get("OPENAI_API_KEY")),
            "DEEPGRAM_API_KEY": bool(os.environ.get("DEEPGRAM_API_KEY")),
            "ELEVENLABS_API_KEY": bool(os.environ.get("ELEVENLABS_API_KEY")),
            "CARTESIA_API_KEY": bool(os.environ.get("CARTESIA_API_KEY")),
        },
    }


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _bool_or_none(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}
