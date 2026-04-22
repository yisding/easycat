"""Chapter 12 — decompose one bundle's turn gap against a budget.

    uv run python docs/teaching/12-evals-and-latency/latency_budget.py \\
        docs/teaching/12-evals-and-latency/bundles/turn_01_fast.bundle

Prints a per-stage actual-vs-budget row for the bundle's main turn.
Budgets are conventions, not limits — they help you *see* drift.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from easycat import load_bundle

# Target: <1000 ms STT-final → bot-done-speaking.
BUDGET_MS = {
    "agent_first_token": 600,
    "tts_total": 400,
    "total": 1000,
}


def analyze(path: Path) -> None:
    bundle = load_bundle(path)
    stt_final_t = None
    first_token_t = None
    tts_total = 0.0
    tts_count = 0
    total_gap = None

    for r in bundle.records():
        if r["name"] == "stt.final" and stt_final_t is None:
            stt_final_t = r["data"].get("t_ms")
        elif r["name"] == "agent.first_token" and first_token_t is None:
            first_token_t = r["data"].get("t_ms")
        elif r["name"] == "stage.tts.execute":
            tts_total += r["data"].get("elapsed_ms", 0.0)
            tts_count += 1
        elif r["name"] == "turn.gap" and total_gap is None:
            total_gap = r["data"].get("total_gap_ms")

    agent_dispatch_ms = (first_token_t - stt_final_t) if (stt_final_t and first_token_t) else None

    print(f"=== {path.name} ===")
    _row("agent first token", agent_dispatch_ms, BUDGET_MS["agent_first_token"])
    _row(f"tts synth ({tts_count} sent.)", tts_total, BUDGET_MS["tts_total"])
    _row("total (stt final → done)", total_gap, BUDGET_MS["total"])


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
