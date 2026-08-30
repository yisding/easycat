"""Serialization and summary projection for latency artifacts."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

from easycat.validation._environment import validation_environment_metadata
from easycat.validation._latency_budgets import (
    DEFAULT_BUDGETS,
    DEFAULT_RELIABILITY_BUDGETS,
    LatencyBudget,
    ReliabilityBudget,
    evaluate_budgets,
    evaluate_reliability_budgets,
)
from easycat.validation._latency_models import (
    LatencyMode,
    LatencyPercentileStats,
    LatencySample,
    LatencyStageDurations,
    ReliabilitySample,
)

__all__ = [
    "append_reliability_sample",
    "build_latency_artifact",
    "build_reliability_artifact",
    "load_latency_samples",
    "load_reliability_samples",
]


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
    # Smoke runs do not have enough samples for meaningful tail enforcement.
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
        "environment": environment or validation_environment_metadata(),
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
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("latency samples payload must be a list")  # noqa: TRY004 domain-specific validation error
    return [
        LatencySample.from_dict(_sample_object(item, kind="latency", index=index))
        for index, item in enumerate(payload)
    ]


def load_reliability_samples(raw: str) -> list[ReliabilitySample]:
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("reliability samples payload must be a list")  # noqa: TRY004 domain-specific validation error
    return [
        ReliabilitySample.from_dict(_sample_object(item, kind="reliability", index=index))
        for index, item in enumerate(payload)
    ]


def _sample_object(value: object, *, kind: str, index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{kind} sample at index {index} must be an object")  # noqa: TRY004 domain-specific validation error
    return value


def append_reliability_sample(path: str | Path, sample: ReliabilitySample) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Use an atomic claim/write to avoid read-modify-write races when concurrent workers append (gh 1036).
    # Fallback to plain write if claim unavailable.
    from easycat.runtime.journal_retention import journal_file_claim

    if destination.exists():
        try:
            payload = json.loads(destination.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError, UnicodeDecodeError):
            payload = []
    else:
        payload = []
    if not isinstance(payload, list):
        payload = []
    payload.append(sample.to_dict())
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        with journal_file_claim(destination, blocking=True) as claimed:
            if claimed:
                # Re-read under lock to avoid lost update
                if destination.exists():
                    try:
                        latest = json.loads(destination.read_text(encoding="utf-8"))
                        if isinstance(latest, list) and len(latest) > len(payload) - 1:
                            # Another writer appended while we waited — merge
                            latest.append(sample.to_dict())
                            encoded = json.dumps(latest, indent=2, sort_keys=True) + "\n"
                    except (json.JSONDecodeError, OSError, ValueError, UnicodeDecodeError):
                        pass
                destination.write_text(encoded, encoding="utf-8")
                return
    except Exception:
        pass
    destination.write_text(encoded, encoding="utf-8")


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


def _summarize_totals(totals: list[float]) -> dict[str, Any]:
    # The separate percentiles block is the single source of tail statistics.
    sorted_totals = sorted(totals)
    return {
        "count": len(sorted_totals),
        "median_ms": median(sorted_totals) if sorted_totals else None,
    }
