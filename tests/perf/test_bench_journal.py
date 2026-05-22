from __future__ import annotations

import json
from pathlib import Path

from perf import bench_journal


def _raw_run(
    *,
    mem_p50: float = 10.0,
    sqlite_p50: float = 20.0,
    mem_sustained: bool = True,
    sqlite_sustained: bool = True,
) -> dict:
    return {
        "meta": {
            "git_sha": "abc123",
            "timestamp": "2026-05-22T12:00:00Z",
            "python": "3.12.0",
            "platform": "Linux-test",
            "runs": 1,
        },
        "in_memory": {
            "append_latency": {
                "count": 5000,
                "p50_us": mem_p50,
                "p90_us": mem_p50 + 3,
                "p99_us": mem_p50 + 6,
                "mean_us": mem_p50 + 1,
                "total_ms": mem_p50 * 5,
            },
            "sustained_rate": {
                "target_rate": 50,
                "target_duration_s": 10,
                "total_writes": 500,
                "dropped": 0 if mem_sustained else 2,
                "elapsed_s": 9.98,
                "actual_rate": 50.1 if mem_sustained else 47.0,
                "sustained": mem_sustained,
            },
            "turn_simulation": {
                "events": 13,
                "total_us": 130.0,
                "per_event_us": 10.0,
            },
        },
        "sqlite": {
            "append_latency": {
                "count": 5000,
                "p50_us": sqlite_p50,
                "p90_us": sqlite_p50 + 3,
                "p99_us": sqlite_p50 + 6,
                "mean_us": sqlite_p50 + 1,
                "total_ms": sqlite_p50 * 5,
            },
            "sustained_rate": {
                "target_rate": 50,
                "target_duration_s": 10,
                "total_writes": 500,
                "dropped": 0 if sqlite_sustained else 2,
                "elapsed_s": 9.98,
                "actual_rate": 50.1 if sqlite_sustained else 47.0,
                "sustained": sqlite_sustained,
            },
            "turn_simulation": {
                "events": 13,
                "total_us": 260.0,
                "per_event_us": 20.0,
            },
        },
    }


def test_validation_artifact_includes_raw_run_and_summary() -> None:
    raw_run = _raw_run()

    artifact = bench_journal.build_validation_artifact(
        raw_run,
        generated_at="2026-05-22T12:00:05Z",
    )

    assert artifact["kind"] == "journal_benchmark_validation"
    assert artifact["schema_version"] == 1
    assert artifact["redaction_version"] == 1
    assert artifact["generated_at"] == "2026-05-22T12:00:05Z"
    assert artifact["raw_run"] == raw_run
    assert artifact["baseline"]["comparison"] == "not_configured"
    assert artifact["summary"] == {
        "status": "pass",
        "runs": 1,
        "backends": {
            "in_memory": {
                "append_latency_p50_us": 10.0,
                "append_latency_p90_us": 13.0,
                "append_latency_p99_us": 16.0,
                "append_latency_mean_us": 11.0,
                "append_latency_total_ms": 50.0,
                "sustained_rate_actual": 50.1,
                "sustained_rate_dropped": 0,
                "sustained_rate_passed": True,
                "turn_simulation_per_event_us": 10.0,
                "turn_simulation_total_us": 130.0,
            },
            "sqlite": {
                "append_latency_p50_us": 20.0,
                "append_latency_p90_us": 23.0,
                "append_latency_p99_us": 26.0,
                "append_latency_mean_us": 21.0,
                "append_latency_total_ms": 100.0,
                "sustained_rate_actual": 50.1,
                "sustained_rate_dropped": 0,
                "sustained_rate_passed": True,
                "turn_simulation_per_event_us": 20.0,
                "turn_simulation_total_us": 260.0,
            },
        },
    }


def test_validation_artifact_compares_against_baseline() -> None:
    raw_run = _raw_run()
    baseline = _raw_run()
    raw_run["in_memory"]["append_latency"]["p50_us"] = 15.0

    artifact = bench_journal.build_validation_artifact(
        raw_run,
        baseline=baseline,
        baseline_source="perf/baseline.json",
        max_regression_percent=25.0,
        generated_at="2026-05-22T12:00:05Z",
    )

    assert artifact["summary"]["status"] == "fail"
    assert artifact["baseline"]["comparison"] == "configured"
    assert artifact["baseline"]["source"] == "perf/baseline.json"
    assert artifact["baseline"]["status"] == "fail"
    assert artifact["baseline"]["max_regression_percent"] == 25.0
    assert artifact["baseline"]["regressions"] == [
        {
            "metric": "in_memory.append_latency.p50_us",
            "current": 15.0,
            "baseline": 10.0,
            "delta": 5.0,
            "delta_percent": 50.0,
        }
    ]


def test_main_writes_raw_output_and_validation_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw_run = _raw_run()
    output_path = tmp_path / "journal-raw.json"
    artifact_path = tmp_path / "journal-artifact.json"

    monkeypatch.setattr(bench_journal, "run_benchmarks", lambda runs=5: raw_run)

    bench_journal.main(
        [
            "--runs",
            "1",
            "--output",
            str(output_path),
            "--artifact",
            str(artifact_path),
        ]
    )

    assert json.loads(output_path.read_text()) == raw_run
    artifact = json.loads(artifact_path.read_text())
    assert artifact["kind"] == "journal_benchmark_validation"
    assert artifact["raw_run"] == raw_run
    assert artifact["summary"]["status"] == "pass"
