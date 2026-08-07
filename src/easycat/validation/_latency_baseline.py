"""Latency baseline comparison policy."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from easycat.validation._latency_models import (
    LatencyComparisonThresholds,
    LatencyPercentileStats,
    LatencySample,
)

__all__ = ["compare_latency_baseline"]


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


def _comparison_samples_by_condition(
    artifact: Mapping[str, Any],
) -> dict[str, list[LatencySample]]:
    grouped: dict[str, list[LatencySample]] = defaultdict(list)
    raw_samples = artifact.get("samples")
    if not isinstance(raw_samples, list):
        raise ValueError("latency comparison samples payload must be a list")  # noqa: TRY004 domain-specific validation error
    for index, item in enumerate(raw_samples):
        if not isinstance(item, dict):
            raise ValueError(  # noqa: TRY004 domain-specific validation error
                f"latency comparison sample at index {index} must be an object"
            )
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

    current_values = [
        value for sample in current_samples if (value := sample.stages.total_ms) is not None
    ]
    baseline_values = [
        value for sample in baseline_samples if (value := sample.stages.total_ms) is not None
    ]
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

    if (
        len(current_values) < thresholds.min_samples
        or len(baseline_values) < thresholds.min_samples
    ):
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
    return {**base_result, "status": "pass"}


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
