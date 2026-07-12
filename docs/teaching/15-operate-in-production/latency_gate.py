"""Gate one metric from ``easycat latency PATH --json``.

Example::

    uv run easycat latency PATH --json \
      | uv run python docs/teaching/15-operate-in-production/latency_gate.py \
          --metric vad->tts --percentile p95 --max-ms 2000 --min-samples 5
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from typing import Any

METRICS = ("vad->stt", "stt->req", "req->token", "token->tts", "vad->tts")
PERCENTILES = ("p50", "p90", "p95", "p99")


def evaluate(
    report: Mapping[str, Any],
    *,
    metric: str,
    percentile: str,
    max_ms: float,
    min_samples: int,
) -> dict[str, Any]:
    """Return a stable pass/fail result for one captured-bundle metric."""
    if report.get("command") != "latency":
        raise ValueError("stdin is not an easycat latency JSON report")
    percentiles = report.get("percentiles")
    if not isinstance(percentiles, Mapping):
        raise ValueError("latency report has no percentiles object")
    stats = percentiles.get(metric)
    if not isinstance(stats, Mapping):
        raise ValueError(f"latency report has no {metric!r} metric")

    count = int(stats.get("count") or 0)
    raw_observed = stats.get(percentile)
    observed_ms = float(raw_observed) if raw_observed is not None else None

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

    if args.max_ms <= 0:
        parser.error("--max-ms must be positive")
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
