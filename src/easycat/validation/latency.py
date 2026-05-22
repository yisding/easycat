from __future__ import annotations

import json
import os
import platform
import sys
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from statistics import median
from typing import Any

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

    def to_dict(self) -> dict[str, float | int]:
        return {
            "relative_regression": self.relative_regression,
            "absolute_regression_ms": self.absolute_regression_ms,
            "min_samples": self.min_samples,
        }


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
) -> dict[str, Any]:
    mode = LatencyMode(mode)
    generated_at = generated_at or datetime.now(UTC)
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
        "summary": _summarize_samples(mode, samples),
    }


def build_reliability_artifact(
    *,
    samples: list[ReliabilitySample],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(UTC)
    return {
        "schema_version": 1,
        "kind": "reliability_validation",
        "generated_at": generated_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "samples": [sample.to_dict() for sample in samples],
        "summary": _summarize_reliability_samples(samples),
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


def _summarize_samples(mode: LatencyMode, samples: list[LatencySample]) -> dict[str, Any]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for sample in samples:
        if sample.warmup or sample.failure_class:
            continue
        if sample.stages.total_ms is not None:
            grouped[sample.condition_id].append(sample.stages.total_ms)

    return {
        condition_id: _summarize_totals(mode, totals)
        for condition_id, totals in sorted(grouped.items())
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
    current_median = median(current_values)
    baseline_median = median(baseline_values)
    delta_ms = current_median - baseline_median
    relative_delta = delta_ms / baseline_median if baseline_median > 0 else None
    relative_regression = (
        relative_delta is not None and relative_delta >= thresholds.relative_regression
    )
    absolute_regression = delta_ms >= thresholds.absolute_regression_ms

    base_result = {
        "condition_id": condition_id,
        "baseline_version": baseline_version,
        "current_count": len(current_values),
        "baseline_count": len(baseline_values),
        "current_median_ms": current_median,
        "baseline_median_ms": baseline_median,
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


def _summarize_totals(mode: LatencyMode, totals: list[float]) -> dict[str, Any]:
    sorted_totals = sorted(totals)
    p50_eligible = mode is LatencyMode.SWEEP and len(sorted_totals) >= 3
    p90_eligible = mode is LatencyMode.SWEEP and len(sorted_totals) >= 10
    p95_eligible = mode is LatencyMode.SWEEP and len(sorted_totals) >= 20
    p99_eligible = mode is LatencyMode.SWEEP and len(sorted_totals) >= 100
    return {
        "count": len(sorted_totals),
        "p50_ms": _percentile_value(sorted_totals, 0.5, p50_eligible),
        "p90_ms": _percentile_value(sorted_totals, 0.9, p90_eligible),
        "p95_ms": _percentile_value(sorted_totals, 0.95, p95_eligible),
        "p99_ms": _percentile_value(sorted_totals, 0.99, p99_eligible),
        "median_ms": median(sorted_totals) if sorted_totals else None,
    }


def _percentile_value(values: list[float], percentile: float, eligible: bool) -> dict[str, Any]:
    value = None
    if values and eligible:
        index = min(len(values) - 1, round((len(values) - 1) * percentile))
        value = values[index]
    return {"eligible": eligible, "value": value}


def classify_latency_failure(message: str) -> str:
    normalized = message.lower().replace("_", " ").replace("-", " ")
    if any(
        token in normalized
        for token in (
            "api key",
            "auth",
            "unauthorized",
            "forbidden",
            "permission denied",
            "401",
            "403",
        )
    ):
        return "provider_auth"
    if any(
        token in normalized
        for token in ("rate limit", "ratelimit", "429", "quota", "too many requests")
    ):
        return "provider_rate_limit"
    if any(token in normalized for token in ("timeout", "timed out", "deadline")):
        return "provider_timeout"
    return "easycat_latency_regression"


def _latency_environment_metadata() -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "ci": os.environ.get("CI") == "true",
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
