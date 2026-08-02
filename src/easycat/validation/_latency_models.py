"""Leaf value objects for latency and reliability validation."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Any, Literal, cast

__all__ = [
    "LatencyComparisonThresholds",
    "LatencyMode",
    "LatencyPercentileStats",
    "LatencySample",
    "LatencyStageDurations",
    "ReliabilitySample",
    "ReliabilitySignals",
]


class LatencyMode(StrEnum):
    SMOKE = "smoke"
    SWEEP = "sweep"


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
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
        # Exclusive (N+1)*p interpolation makes tail samples visible. Clamp
        # small-sample extrapolation to values that were actually observed.
        cuts = statistics.quantiles(cleaned, n=100, method="exclusive")
        lower = min(cleaned)
        upper = max(cleaned)
        return cls(
            count=count,
            p50=_clamp_percentile(cuts[49], lower, upper),
            p90=_clamp_percentile(cuts[89], lower, upper),
            p95=_clamp_percentile(cuts[94], lower, upper),
            p99=_clamp_percentile(cuts[98], lower, upper),
        )


def _clamp_percentile(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


@dataclass(frozen=True, slots=True)
class LatencyStageDurations:
    detection_ms: float | None = None
    stt_ms: float | None = None
    stt_finalize_close_ms: float | None = None
    agent_request_start_ms: float | None = None
    llm_ttft_ms: float | None = None
    tts_ttfb_ms: float | None = None
    transport_ms: float | None = None
    interruption_cutoff_ms: float | None = None
    total_ms: float | None = None

    def to_dict(self) -> dict[str, float | None]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass(frozen=True, slots=True)
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
            warmup=_bool_from_payload(payload.get("warmup", False)),
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


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
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
            informational=_bool_from_payload(payload.get("informational", True)),
            eligible=_bool_from_payload(payload.get("eligible", False)),
            signals=ReliabilitySignals.from_dict(signals),
        )


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(cast(float, value))
    except OverflowError as exc:
        raise ValueError("latency and reliability numeric values must be finite") from exc
    if not math.isfinite(parsed):
        raise ValueError("latency and reliability numeric values must be finite")
    return parsed


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("reliability integer values must be finite integers")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError("reliability integer values must be finite integers")
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError("reliability integer values must be finite integers") from exc
    raise ValueError("reliability integer values must be finite integers")


def _bool_or_none(value: object) -> bool | None:
    if value is None:
        return None
    return _bool_from_payload(value)


def _bool_from_payload(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"", "0", "false", "no", "off"}:
            return False
    return bool(value)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}
