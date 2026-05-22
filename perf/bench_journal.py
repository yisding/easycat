#!/usr/bin/env python3
"""Journal performance benchmark.

Measures:
- STT partial-transcript write rate (target: 50/s sustained for 10s)
- P50 and P90 single-record append latency
- Bulk write throughput for both in-memory and SQLite backends

Results are written to perf/baseline.json.

Usage:
    uv run python perf/bench_journal.py
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import subprocess
import tempfile
import time
from argparse import ArgumentParser
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from easycat.runtime.journal import InMemoryRingBuffer, SqliteJournal
from easycat.runtime.records import JournalRecordKind

_DEFAULT_REGRESSION_THRESHOLD_PERCENT = 25.0
_LATENCY_METRICS = (
    ("append_latency", "p50_us"),
    ("append_latency", "p90_us"),
    ("append_latency", "p99_us"),
    ("append_latency", "mean_us"),
    ("append_latency", "total_ms"),
    ("turn_simulation", "per_event_us"),
    ("turn_simulation", "total_us"),
)
_RATE_METRICS = (("sustained_rate", "actual_rate"),)
_COUNT_METRICS = (("sustained_rate", "dropped"),)


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()[:12]
    except Exception:
        return "unknown"


def bench_append_latency(backend, session_id: str, n: int = 5000) -> dict:
    """Append n records and measure per-record latency."""
    latencies_ns: list[int] = []
    for i in range(n):
        t0 = time.monotonic_ns()
        backend.append(
            kind=JournalRecordKind.EVENT,
            name="stt_partial",
            session_id=session_id,
            data={"text": f"partial transcript word {i}", "is_final": False},
        )
        latencies_ns.append(time.monotonic_ns() - t0)

    latencies_us = [ns / 1000 for ns in latencies_ns]
    return {
        "count": n,
        "p50_us": round(statistics.median(latencies_us), 2),
        "p90_us": round(sorted(latencies_us)[int(n * 0.9)], 2),
        "p99_us": round(sorted(latencies_us)[int(n * 0.99)], 2),
        "mean_us": round(statistics.mean(latencies_us), 2),
        "total_ms": round(sum(latencies_us) / 1000, 2),
    }


def bench_sustained_rate(backend, session_id: str, rate: int = 50, duration_s: int = 10) -> dict:
    """Sustain `rate` writes/sec for `duration_s` seconds."""
    total = rate * duration_s
    interval = 1.0 / rate
    dropped = 0
    t_start = time.monotonic()

    for i in range(total):
        target = t_start + i * interval
        now = time.monotonic()
        if now < target:
            # Busy-wait for sub-ms precision.
            while time.monotonic() < target:
                pass

        seq = backend.append(
            kind=JournalRecordKind.EVENT,
            name="stt_partial",
            session_id=session_id,
            data={"text": f"word {i}", "seq": i},
        )
        if seq == -1:
            dropped += 1

    elapsed = time.monotonic() - t_start
    actual_rate = total / elapsed
    return {
        "target_rate": rate,
        "target_duration_s": duration_s,
        "total_writes": total,
        "dropped": dropped,
        "elapsed_s": round(elapsed, 3),
        "actual_rate": round(actual_rate, 1),
        "sustained": actual_rate >= rate * 0.95,
    }


def bench_turn_simulation(backend, session_id: str) -> dict:
    """Simulate a single voice turn: STT partials → final → agent → TTS."""
    events = [
        ("stt_partial", {"text": "hello"}),
        ("stt_partial", {"text": "hello how"}),
        ("stt_partial", {"text": "hello how are"}),
        ("stt_partial", {"text": "hello how are you"}),
        ("stt_final", {"text": "hello how are you", "is_final": True}),
        ("agent_start", {"turn_id": "t1"}),
        ("agent_delta", {"text": "I'm doing"}),
        ("agent_delta", {"text": " great, thanks!"}),
        ("agent_final", {"text": "I'm doing great, thanks!"}),
        ("tts_start", {"text": "I'm doing great, thanks!"}),
        ("tts_audio", {"bytes": 4800}),
        ("tts_audio", {"bytes": 4800}),
        ("tts_done", {}),
    ]

    t0 = time.monotonic_ns()
    for name, data in events:
        backend.append(
            kind=JournalRecordKind.EVENT,
            name=name,
            session_id=session_id,
            data=data,
        )
    elapsed_us = (time.monotonic_ns() - t0) / 1000

    return {
        "events": len(events),
        "total_us": round(elapsed_us, 2),
        "per_event_us": round(elapsed_us / len(events), 2),
    }


def run_benchmarks(runs: int = 5) -> dict:
    """Run all benchmarks, return results dict."""
    results: dict = {
        "meta": {
            "git_sha": _git_sha(),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "runs": runs,
        },
        "in_memory": {},
        "sqlite": {},
    }

    # ── In-memory backend ────────────────────────────────────────
    print("Benchmarking InMemoryRingBuffer...")
    latencies = []
    rates = []
    turns = []
    for i in range(runs):
        buf = InMemoryRingBuffer(capacity=100_000)
        sid = f"bench-mem-{i}"
        latencies.append(bench_append_latency(buf, sid))
        buf2 = InMemoryRingBuffer(capacity=100_000)
        rates.append(bench_sustained_rate(buf2, sid))
        buf3 = InMemoryRingBuffer(capacity=100_000)
        turns.append(bench_turn_simulation(buf3, sid))

    results["in_memory"]["append_latency"] = _median_run(latencies)
    results["in_memory"]["sustained_rate"] = _median_run(rates)
    results["in_memory"]["turn_simulation"] = _median_run(turns)

    # ── SQLite backend ───────────────────────────────────────────
    print("Benchmarking SqliteJournal...")
    latencies = []
    rates = []
    turns = []
    for i in range(runs):
        tmpdir = tempfile.mkdtemp()
        sid = f"bench-sql-{i}"
        j = SqliteJournal(sid, data_dir=tmpdir)
        latencies.append(bench_append_latency(j, sid))
        j.close()

        j2 = SqliteJournal(f"{sid}-rate", data_dir=tmpdir)
        rates.append(bench_sustained_rate(j2, f"{sid}-rate"))
        j2.close()

        j3 = SqliteJournal(f"{sid}-turn", data_dir=tmpdir)
        turns.append(bench_turn_simulation(j3, f"{sid}-turn"))
        j3.close()

    results["sqlite"]["append_latency"] = _median_run(latencies)
    results["sqlite"]["sustained_rate"] = _median_run(rates)
    results["sqlite"]["turn_simulation"] = _median_run(turns)

    return results


def build_validation_artifact(
    raw_run: dict[str, Any],
    *,
    baseline: dict[str, Any] | None = None,
    baseline_source: str | None = None,
    max_regression_percent: float = _DEFAULT_REGRESSION_THRESHOLD_PERCENT,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Wrap a raw journal benchmark run in a validation artifact envelope."""

    summary = _summarize_run(raw_run)
    baseline_payload = {"comparison": "not_configured"}
    if baseline is not None:
        comparison = _compare_to_baseline(
            raw_run,
            _extract_raw_run(baseline),
            max_regression_percent=max_regression_percent,
        )
        baseline_payload = {
            "comparison": "configured",
            "source": baseline_source or "inline",
            "status": comparison["status"],
            "max_regression_percent": max_regression_percent,
            "regressions": comparison["regressions"],
        }
        if comparison["status"] == "fail":
            summary["status"] = "fail"

    return {
        "kind": "journal_benchmark_validation",
        "schema_version": 1,
        "redaction_version": 1,
        "generated_at": generated_at or _utc_timestamp(),
        "summary": summary,
        "baseline": baseline_payload,
        "raw_run": raw_run,
    }


def _summarize_run(raw_run: dict[str, Any]) -> dict[str, Any]:
    backends: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []

    for backend_name in ("in_memory", "sqlite"):
        backend = raw_run.get(backend_name, {})
        append_latency = backend.get("append_latency", {})
        sustained_rate = backend.get("sustained_rate", {})
        turn_simulation = backend.get("turn_simulation", {})
        sustained = bool(sustained_rate.get("sustained", False))

        backends[backend_name] = {
            "append_latency_p50_us": append_latency.get("p50_us"),
            "append_latency_p90_us": append_latency.get("p90_us"),
            "append_latency_p99_us": append_latency.get("p99_us"),
            "append_latency_mean_us": append_latency.get("mean_us"),
            "append_latency_total_ms": append_latency.get("total_ms"),
            "sustained_rate_actual": sustained_rate.get("actual_rate"),
            "sustained_rate_dropped": sustained_rate.get("dropped"),
            "sustained_rate_passed": sustained,
            "turn_simulation_per_event_us": turn_simulation.get("per_event_us"),
            "turn_simulation_total_us": turn_simulation.get("total_us"),
        }
        if not sustained:
            failures.append(
                {
                    "metric": f"{backend_name}.sustained_rate.sustained",
                    "message": "sustained write-rate target was not met",
                }
            )

    summary: dict[str, Any] = {
        "status": "fail" if failures else "pass",
        "runs": raw_run.get("meta", {}).get("runs"),
        "backends": backends,
    }
    if failures:
        summary["failures"] = failures
    return summary


def _compare_to_baseline(
    current: dict[str, Any],
    baseline: dict[str, Any],
    *,
    max_regression_percent: float,
) -> dict[str, Any]:
    regressions: list[dict[str, Any]] = []

    for backend_name in ("in_memory", "sqlite"):
        current_backend = current.get(backend_name, {})
        baseline_backend = baseline.get(backend_name, {})

        for group, metric in _LATENCY_METRICS:
            regression = _higher_is_worse_regression(
                metric_path=f"{backend_name}.{group}.{metric}",
                current=_metric_value(current_backend, group, metric),
                baseline=_metric_value(baseline_backend, group, metric),
                max_regression_percent=max_regression_percent,
            )
            if regression is not None:
                regressions.append(regression)

        for group, metric in _RATE_METRICS:
            regression = _lower_is_worse_regression(
                metric_path=f"{backend_name}.{group}.{metric}",
                current=_metric_value(current_backend, group, metric),
                baseline=_metric_value(baseline_backend, group, metric),
                max_regression_percent=max_regression_percent,
            )
            if regression is not None:
                regressions.append(regression)

        for group, metric in _COUNT_METRICS:
            regression = _count_regression(
                metric_path=f"{backend_name}.{group}.{metric}",
                current=_metric_value(current_backend, group, metric),
                baseline=_metric_value(baseline_backend, group, metric),
            )
            if regression is not None:
                regressions.append(regression)

        if (
            baseline_backend.get("sustained_rate", {}).get("sustained") is True
            and current_backend.get("sustained_rate", {}).get("sustained") is False
        ):
            regressions.append(
                {
                    "metric": f"{backend_name}.sustained_rate.sustained",
                    "current": False,
                    "baseline": True,
                    "delta": -1,
                    "delta_percent": 100.0,
                }
            )

    return {
        "status": "fail" if regressions else "pass",
        "regressions": regressions,
    }


def _higher_is_worse_regression(
    *,
    metric_path: str,
    current: Any,
    baseline: Any,
    max_regression_percent: float,
) -> dict[str, Any] | None:
    current_number = _coerce_number(current)
    baseline_number = _coerce_number(baseline)
    if current_number is None or baseline_number is None:
        return None

    delta = current_number - baseline_number
    delta_percent = _delta_percent(delta, baseline_number)
    if delta <= 0 or delta_percent is None or delta_percent <= max_regression_percent:
        return None
    return _regression_payload(metric_path, current_number, baseline_number, delta, delta_percent)


def _lower_is_worse_regression(
    *,
    metric_path: str,
    current: Any,
    baseline: Any,
    max_regression_percent: float,
) -> dict[str, Any] | None:
    current_number = _coerce_number(current)
    baseline_number = _coerce_number(baseline)
    if current_number is None or baseline_number is None:
        return None

    delta = current_number - baseline_number
    delta_percent = _delta_percent(-delta, baseline_number)
    if delta >= 0 or delta_percent is None or delta_percent <= max_regression_percent:
        return None
    return _regression_payload(metric_path, current_number, baseline_number, delta, delta_percent)


def _count_regression(
    *,
    metric_path: str,
    current: Any,
    baseline: Any,
) -> dict[str, Any] | None:
    current_number = _coerce_number(current)
    baseline_number = _coerce_number(baseline)
    if current_number is None or baseline_number is None or current_number <= baseline_number:
        return None

    delta = current_number - baseline_number
    delta_percent = _delta_percent(delta, baseline_number)
    return _regression_payload(metric_path, current_number, baseline_number, delta, delta_percent)


def _regression_payload(
    metric_path: str,
    current: float,
    baseline: float,
    delta: float,
    delta_percent: float | None,
) -> dict[str, Any]:
    return {
        "metric": metric_path,
        "current": _round_number(current),
        "baseline": _round_number(baseline),
        "delta": _round_number(delta),
        "delta_percent": _round_number(delta_percent) if delta_percent is not None else None,
    }


def _metric_value(backend: dict[str, Any], group: str, metric: str) -> Any:
    return backend.get(group, {}).get(metric)


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _delta_percent(delta: float, baseline: float) -> float | None:
    if baseline == 0:
        return None
    return (delta / baseline) * 100


def _round_number(value: float) -> float:
    rounded = round(value, 2)
    if rounded == -0.0:
        return 0.0
    return rounded


def _extract_raw_run(payload: dict[str, Any]) -> dict[str, Any]:
    raw_run = payload.get("raw_run")
    if isinstance(raw_run, dict):
        return raw_run
    return payload


def _median_run(runs: list[dict]) -> dict:
    """Pick the median run by total latency or elapsed time."""
    # Sort by whichever key represents total cost.
    r0 = runs[0]
    key = "total_ms" if "total_ms" in r0 else ("elapsed_s" if "elapsed_s" in r0 else "total_us")
    by_total = sorted(runs, key=lambda r: r[key])
    return by_total[len(by_total) // 2]


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    results = run_benchmarks(runs=args.runs)

    out_path = Path(args.output)
    _write_json(out_path, results)
    print(f"\nResults written to {out_path}")

    if args.artifact:
        baseline = _load_json(Path(args.baseline)) if args.baseline else None
        artifact = build_validation_artifact(
            results,
            baseline=baseline,
            baseline_source=args.baseline,
            max_regression_percent=args.max_regression_percent,
        )
        artifact_path = Path(args.artifact)
        _write_json(artifact_path, artifact, sort_keys=True)
        print(f"Validation artifact written to {artifact_path}")

    print(json.dumps(results, indent=2))


def _parse_args(argv: Sequence[str] | None) -> Any:
    parser = ArgumentParser(description="Benchmark EasyCat journal write performance.")
    parser.add_argument(
        "--runs",
        type=int,
        default=5,
        help="Number of benchmark runs per backend.",
    )
    parser.add_argument(
        "--output",
        default=os.path.join(os.path.dirname(__file__), "baseline.json"),
        help="Path for the raw benchmark JSON. Defaults to perf/baseline.json.",
    )
    parser.add_argument(
        "--artifact",
        help="Optional path for the validation-compatible benchmark artifact JSON.",
    )
    parser.add_argument(
        "--baseline",
        help="Optional raw benchmark or journal benchmark artifact JSON to compare against.",
    )
    parser.add_argument(
        "--max-regression-percent",
        type=float,
        default=_DEFAULT_REGRESSION_THRESHOLD_PERCENT,
        help="Allowed percent regression before baseline comparison status fails.",
    )
    return parser.parse_args(argv)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _write_json(path: Path, payload: dict[str, Any], *, sort_keys: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=sort_keys) + "\n")


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    main()
