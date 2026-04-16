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

from easycat.runtime.journal import InMemoryRingBuffer, SqliteJournal
from easycat.runtime.records import JournalRecordKind


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


def _median_run(runs: list[dict]) -> dict:
    """Pick the median run by total latency or elapsed time."""
    # Sort by whichever key represents total cost.
    r0 = runs[0]
    key = "total_ms" if "total_ms" in r0 else ("elapsed_s" if "elapsed_s" in r0 else "total_us")
    by_total = sorted(runs, key=lambda r: r[key])
    return by_total[len(by_total) // 2]


def main() -> None:
    results = run_benchmarks(runs=5)

    out_path = os.path.join(os.path.dirname(__file__), "baseline.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults written to {out_path}")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
