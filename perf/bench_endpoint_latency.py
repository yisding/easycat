#!/usr/bin/env python3
"""Benchmark EasyCat fixed versus punctuation-aware endpoint latency."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any

from easycat.events import EventBus, TurnEnded, VADStartSpeaking, VADStopSpeaking
from easycat.turn_manager import TurnManager, TurnManagerConfig


def _percentile(samples: list[float], percentile: float) -> float:
    """Return a nearest-rank percentile from a non-empty sample set."""
    if not samples:
        raise ValueError("samples must not be empty")
    ordered = sorted(samples)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def summarize(samples: list[float]) -> dict[str, float]:
    """Summarize endpoint samples in milliseconds."""
    if not samples:
        raise ValueError("samples must not be empty")
    return {
        "p50_ms": statistics.median(samples),
        "p90_ms": _percentile(samples, 0.90),
        "mean_ms": statistics.fmean(samples),
        "min_ms": min(samples),
        "max_ms": max(samples),
    }


def compare(baseline: list[float], punctuated: list[float]) -> dict[str, Any]:
    """Return distributions and the p50 latency reduction."""
    baseline_summary = summarize(baseline)
    punctuated_summary = summarize(punctuated)
    baseline_p50_ms = baseline_summary["p50_ms"]
    if baseline_p50_ms <= 0:
        raise ValueError("baseline p50 must be positive")
    saved_ms = baseline_p50_ms - punctuated_summary["p50_ms"]
    return {
        "schema_version": 1,
        "fixed": baseline_summary,
        "punctuated": punctuated_summary,
        "p50_saved_ms": saved_ms,
        "p50_reduction_percent": saved_ms / baseline_p50_ms * 100.0,
    }


async def _sample(*, punctuated: bool, full_ms: int, punctuated_ms: int) -> float:
    """Measure one end-to-end endpoint transition in milliseconds."""
    bus = EventBus()
    ended = asyncio.Event()
    ended_at = 0.0

    def _on_turn_ended(event: TurnEnded) -> None:
        nonlocal ended_at
        ended_at = event.timestamp
        ended.set()

    bus.subscribe(TurnEnded, _on_turn_ended)
    manager = TurnManager(
        bus,
        config=TurnManagerConfig(
            end_of_turn_silence_ms=full_ms,
            punctuated_end_of_turn_silence_ms=punctuated_ms,
        ),
    )
    try:
        await manager.on_vad_event(VADStartSpeaking())
        started = time.monotonic()
        await manager.on_vad_event(VADStopSpeaking())
        if punctuated:
            manager.on_stt_final(
                "The request is complete.",
                pause_generation=manager.pause_generation,
            )
        await asyncio.wait_for(ended.wait(), timeout=full_ms / 1000.0 + 1.0)
        return (ended_at - started) * 1000.0
    finally:
        await manager.shutdown()


async def run(*, samples: int, full_ms: int, punctuated_ms: int) -> dict[str, Any]:
    """Collect fixed and punctuated latency samples and compare them."""
    if samples < 1:
        raise ValueError("samples must be positive")
    if full_ms <= 0:
        raise ValueError("full_ms must be positive")
    if punctuated_ms < 0:
        raise ValueError("punctuated_ms must be non-negative")
    baseline: list[float] = []
    punctuated: list[float] = []
    for _ in range(samples):
        baseline.append(
            await _sample(punctuated=False, full_ms=full_ms, punctuated_ms=punctuated_ms)
        )
        punctuated.append(
            await _sample(punctuated=True, full_ms=full_ms, punctuated_ms=punctuated_ms)
        )
    payload = compare(baseline, punctuated)
    payload.update(
        {
            "samples": samples,
            "configured_fixed_ms": full_ms,
            "configured_punctuated_ms": punctuated_ms,
        }
    )
    return payload


def main() -> None:
    """Run the endpoint benchmark CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--fixed-ms", type=int, default=500)
    parser.add_argument("--punctuated-ms", type=int, default=200)
    parser.add_argument("--require-faster", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples < 1 or args.fixed_ms <= 0 or args.punctuated_ms < 0:
        parser.error(
            "samples and fixed delay must be positive; punctuated delay must be non-negative"
        )

    payload = asyncio.run(
        run(
            samples=args.samples,
            full_ms=args.fixed_ms,
            punctuated_ms=args.punctuated_ms,
        )
    )
    if args.require_faster and payload["p50_saved_ms"] <= 0:
        raise SystemExit("punctuation-aware endpointing was not faster")
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
