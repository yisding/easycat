"""Leaf value objects for latency and reliability validation."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Any, Literal

from easycat._numeric import is_finite_number

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

    def __post_init__(self) -> None:
        for name in ("relative_regression", "absolute_regression_ms"):
            value = getattr(self, name)
            if not is_finite_number(value) or value < 0:
                raise ValueError(f"{name} must be a finite number >= 0")
        if isinstance(self.min_samples, bool) or not isinstance(self.min_samples, int):
            raise ValueError("min_samples must be an integer >= 1")  # noqa: TRY004 domain-specific validation error
        if self.min_samples < 1:
            raise ValueError("min_samples must be >= 1")
        if self.regression_percentile not in ("p50", "p90", "p95", "p99"):
            raise ValueError(
                "regression_percentile must be one of p50, p90, p95, p99; "
                f"got {self.regression_percentile!r}"
            )

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

    def __post_init__(self) -> None:
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 0:
            raise ValueError("latency percentile count must be an integer >= 0")
        for item in fields(self):
            if item.name == "count":
                continue
            _require_non_negative_number(
                f"latency percentile {item.name}",
                getattr(self, item.name),
            )

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
        cleaned = [
            parsed
            for value in values
            if (parsed := _float_or_none(value, name="latency percentile value")) is not None
        ]
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

    def __post_init__(self) -> None:
        for item in fields(self):
            _require_non_negative_number(item.name, getattr(self, item.name))

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
        stages = payload.get("stages", {})
        if not isinstance(stages, dict):
            raise ValueError("latency sample stages must be an object")  # noqa: TRY004 domain-specific validation error
        return cls(
            sample_id=_string_from_payload(payload["sample_id"], name="sample_id"),
            condition_id=_string_from_payload(payload["condition_id"], name="condition_id"),
            warmup=_bool_from_payload(payload.get("warmup", False)),
            timestamp_source=_string_from_payload(
                payload.get("timestamp_source", "unknown"),
                name="timestamp_source",
            ),
            provider=_string_dict(payload.get("provider"), name="provider"),
            model=_string_dict(payload.get("model"), name="model"),
            transport=_string_dict(payload.get("transport"), name="transport"),
            debug=_string_dict(payload.get("debug"), name="debug"),
            stages=LatencyStageDurations(
                **{
                    item.name: _float_or_none(stages.get(item.name))
                    for item in fields(LatencyStageDurations)
                }
            ),
            missing_stage_reason=_optional_string(
                payload.get("missing_stage_reason"),
                name="missing_stage_reason",
            ),
            failure_class=_optional_string(
                payload.get("failure_class"),
                name="failure_class",
            ),
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

    def __post_init__(self) -> None:
        _require_non_negative_number("event_loop_lag_ms", self.event_loop_lag_ms)
        for name in (
            "queue_depth",
            "dropped_frames",
            "active_sessions",
            "memory_growth_kib",
        ):
            _require_non_negative_integer(name, getattr(self, name))

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
            unavailable_reason=_optional_string(
                payload.get("unavailable_reason"),
                name="unavailable_reason",
            ),
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
        signals = payload.get("signals", {})
        if not isinstance(signals, dict):
            raise ValueError("reliability sample signals must be an object")  # noqa: TRY004 domain-specific validation error
        return cls(
            sample_id=_string_from_payload(payload["sample_id"], name="sample_id"),
            condition_id=_string_from_payload(payload["condition_id"], name="condition_id"),
            mode=_string_from_payload(payload.get("mode", "unknown"), name="mode"),
            informational=_bool_from_payload(payload.get("informational", True)),
            eligible=_bool_from_payload(payload.get("eligible", False)),
            signals=ReliabilitySignals.from_dict(signals),
        )


def _require_non_negative_number(name: str, value: object) -> None:
    if value is None:
        return
    if not is_finite_number(value) or value < 0:
        raise ValueError(f"{name} must be a finite number >= 0")


def _require_non_negative_integer(name: str, value: object) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not is_finite_number(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a finite integer >= 0")


def _float_or_none(
    value: object,
    *,
    name: str = "latency and reliability numeric value",
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number >= 0")  # noqa: TRY004 domain-specific validation error
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number >= 0") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{name} must be a finite number >= 0")
    return parsed


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    parsed: int
    if isinstance(value, bool):
        raise ValueError("reliability integer values must be finite integers >= 0")  # noqa: TRY004 domain-specific validation error
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError("reliability integer values must be finite integers >= 0")
        parsed = int(value)
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except (ValueError, OverflowError) as exc:
            raise ValueError("reliability integer values must be finite integers >= 0") from exc
    else:
        raise ValueError("reliability integer values must be finite integers >= 0")  # noqa: TRY004 domain-specific validation error
    if not is_finite_number(parsed) or parsed < 0:
        raise ValueError("reliability integer values must be finite integers >= 0")
    return parsed


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
    raise ValueError("latency and reliability boolean values must be booleans or boolean strings")


def _string_from_payload(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"latency and reliability {name} must be a string")  # noqa: TRY004 domain-specific validation error
    return value


def _optional_string(value: object, *, name: str) -> str | None:
    if value is None:
        return None
    return _string_from_payload(value, name=name)


def _string_dict(value: object, *, name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"latency sample {name} must be an object")  # noqa: TRY004 domain-specific validation error
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise ValueError(f"latency sample {name} entries must be strings")
    return dict(value)
