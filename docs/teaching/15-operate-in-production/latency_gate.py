"""Gate one metric from ``easycat latency PATH --json``.

Example::

    uv run easycat latency PATH --json \
      | uv run python docs/teaching/15-operate-in-production/latency_gate.py \
          --metric 'vad->tts' --percentile p95 --max-ms 2000 --min-samples 5
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping
from typing import Any

METRICS = ("vad->stt", "stt->req", "req->token", "token->tts", "vad->tts")
PERCENTILES = ("p50", "p90", "p95", "p99")


def _sample_count(stats: Mapping[str, Any]) -> int:
    raw_count = stats.get("count")
    if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0:
        raise ValueError("latency metric count must be a non-negative integer")
    return raw_count


def _observed_ms(stats: Mapping[str, Any], percentile: str) -> float | None:
    raw_observed = stats.get(percentile)
    if raw_observed is None:
        return None
    if isinstance(raw_observed, bool) or not isinstance(raw_observed, (int, float)):
        raise ValueError(f"latency metric {percentile} must be a number or null")  # noqa: TRY004 domain-specific validation error
    observed_ms = float(raw_observed)
    if not math.isfinite(observed_ms) or observed_ms < 0:
        raise ValueError(f"latency metric {percentile} must be a finite non-negative number")
    return observed_ms


def evaluate(
    report: Mapping[str, Any],
    *,
    metric: str,
    percentile: str,
    max_ms: float,
    min_samples: int,
) -> dict[str, Any]:
    """Return a stable pass/fail result for one captured-bundle metric."""
    if not math.isfinite(max_ms) or max_ms <= 0:
        raise ValueError("max_ms must be finite and positive")
    if min_samples <= 0:
        raise ValueError("min_samples must be positive")
    if not isinstance(report, Mapping):
        raise ValueError("stdin is not a JSON object")  # noqa: TRY004 domain-specific validation error
    if report.get("command") != "latency":
        raise ValueError("stdin is not an easycat latency JSON report")
    percentiles = report.get("percentiles")
    if not isinstance(percentiles, Mapping):
        raise ValueError("latency report has no percentiles object")  # noqa: TRY004 domain-specific validation error
    stats = percentiles.get(metric)
    if not isinstance(stats, Mapping):
        raise ValueError(f"latency report has no {metric!r} metric")  # noqa: TRY004 domain-specific validation error

    count = _sample_count(stats)
    observed_ms = _observed_ms(stats, percentile)

    if count < min_samples:
        status = "fail"
        reason = "insufficient_samples"
    elif observed_ms is None:
        status = "fail"
        reason = "missing_percentile"
    elif observed_ms > max_ms:
        status = "fail"
        reason = "over_budget"
    else:
        status = "pass"
        reason = "within_budget"

    return {
        "status": status,
        "reason": reason,
        "path": report.get("path"),
        "metric": metric,
        "percentile": percentile,
        "observed_ms": observed_ms,
        "max_ms": float(max_ms),
        "sample_count": count,
        "min_samples": min_samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metric", choices=METRICS, default="vad->tts")
    parser.add_argument("--percentile", choices=PERCENTILES, default="p95")
    parser.add_argument("--max-ms", type=float, required=True)
    parser.add_argument("--min-samples", type=int, default=20)
    args = parser.parse_args()

    if not math.isfinite(args.max_ms) or args.max_ms <= 0:
        parser.error("--max-ms must be finite and positive")
    if args.min_samples <= 0:
        parser.error("--min-samples must be positive")

    try:
        report = json.load(sys.stdin)
        result = evaluate(
            report,
            metric=args.metric,
            percentile=args.percentile,
            max_ms=args.max_ms,
            min_samples=args.min_samples,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "error", "reason": "invalid_report", "message": str(exc)}))
        return 2

    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
