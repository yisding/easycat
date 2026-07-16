"""Chapter 12 — decompose one bundle's turn gap against a budget.

    uv run python docs/teaching/12-evals-and-latency/latency_budget.py \\
        docs/teaching/12-evals-and-latency/bundles/turn_01_fast.bundle

Prints the first-audio critical path for the bundle's main turn.
Budgets are conventions, not limits — they help you *see* drift.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from easycat.debug.testing import load_bundle

# Target: <1000 ms STT-final → first accepted audio.
BUDGET_MS = {
    "stt_final_to_first_token": 600,
    "first_token_to_audio": 400,
    "total": 1000,
}


def measure(path: Path) -> dict[str, float | None]:
    """Return the three first-audio critical-path measurements."""
    bundle = load_bundle(path)
    stt_final_t = None
    first_token_t = None
    first_audio_t = None
    total_gap = None

    for r in bundle.records():
        if r["name"] == "stt.final" and stt_final_t is None:
            stt_final_t = r["data"].get("t_ms")
        elif r["name"] == "agent.first_token" and first_token_t is None:
            first_token_t = r["data"].get("t_ms")
        elif r["name"] == "tts.first_audio" and first_audio_t is None:
            first_audio_t = r["data"].get("t_ms")
        elif r["name"] == "turn.gap" and total_gap is None:
            total_gap = r["data"].get("total_gap_ms")

    agent_dispatch_ms = (
        first_token_t - stt_final_t
        if stt_final_t is not None and first_token_t is not None
        else None
    )
    first_token_to_audio_ms = (
        first_audio_t - first_token_t
        if first_audio_t is not None and first_token_t is not None
        else None
    )
    return {
        "stt_final_to_first_token_ms": agent_dispatch_ms,
        "first_token_to_audio_ms": first_token_to_audio_ms,
        "first_audio_gap_ms": total_gap,
    }


def analyze(path: Path) -> None:
    metrics = measure(path)

    print(f"=== {path.name} ===")
    _row(
        "stt final → first token",
        metrics["stt_final_to_first_token_ms"],
        BUDGET_MS["stt_final_to_first_token"],
    )
    _row(
        "first token → first audio",
        metrics["first_token_to_audio_ms"],
        BUDGET_MS["first_token_to_audio"],
    )
    _row(
        "stt final → first audio",
        metrics["first_audio_gap_ms"],
        BUDGET_MS["total"],
    )


def _row(label: str, actual: float | None, budget: float) -> None:
    if actual is None:
        print(f"  {label:28} {'(missing)':>10}     budget {budget:>5} ms")
        return
    marker = "OK" if actual <= budget else "OVER"
    print(f"  {label:28} {actual:>8.0f} ms     budget {budget:>5} ms    {marker}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bundles", type=Path, nargs="+")
    args = ap.parse_args()
    missing = [p for p in args.bundles if not p.exists()]
    if missing:
        sys.exit(f"Missing: {missing}. Run generate_bundles.py first.")
    for p in args.bundles:
        analyze(p)


if __name__ == "__main__":
    main()
