"""Chapter 15 — translate ch 13's production-shape bundle to ch 12.

Ch 13 calls ``create_session()`` directly, so its journal uses the
real runtime's **production shape**: paired ``stage_start`` /
``stage_complete`` records you have to match up to recover per-stage
timing. Ch 12's eval scripts key on the **teaching shape** —
composite records named ``stage.<name>.execute`` with an
``elapsed_ms`` field baked in.

This ~30-line translator closes the gap. It walks a ch 13 bundle's
records in sequence order; every time it sees a stage_start for a
stage+turn, it holds it open; every time it sees the paired
stage_complete, it emits a single composite record with the elapsed
wall-clock time. The output NDJSON file is something ch 12's
evals.py / latency_budget.py can consume directly if you stage it
into a fresh bundle.

    uv run python docs/teaching/15-operate-in-production/translate.py \\
        path/to/ch13-openai-local.bundle \\
        out.ndjson
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from easycat.debug.testing import load_bundle


def translate(bundle_path: Path) -> list[dict]:
    bundle = load_bundle(bundle_path)
    open_starts: dict[tuple[str | None, str], dict] = {}
    out: list[dict] = []
    for rec in bundle.records():
        name = rec.get("name")
        data = rec.get("data") or {}
        stage = data.get("stage")
        turn = rec.get("turn_id")
        if name == "stage_start" and stage:
            open_starts[(turn, stage)] = rec
        elif name == "stage_complete" and stage:
            start = open_starts.pop((turn, stage), None)
            if start is None:
                continue  # dangling complete — the start was filtered out upstream
            elapsed_ms = _elapsed_ms(start, rec)
            out.append(
                {
                    "sequence": rec.get("sequence"),
                    "turn_id": turn,
                    "name": f"stage.{stage}.execute",
                    "data": {
                        "stage": stage,
                        "elapsed_ms": elapsed_ms,
                        "state_before": (start.get("data") or {}).get("state_before"),
                        "state_after": data.get("state_after"),
                    },
                }
            )
    return out


def _elapsed_ms(start: dict, complete: dict) -> float:
    """Difference between paired timings in ms. Prefer mono_ns; fall
    back to wall_ns; default 0 if neither is populated."""
    start_t = (start.get("timing") or {}).get("mono_ns") or (start.get("timing") or {}).get(
        "wall_ns"
    )
    end_t = (complete.get("timing") or {}).get("mono_ns") or (complete.get("timing") or {}).get(
        "wall_ns"
    )
    if start_t is None or end_t is None:
        return 0.0
    return (int(end_t) - int(start_t)) / 1e6


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle", type=Path, help="ch 13 bundle to translate")
    ap.add_argument("out", type=Path, help="output NDJSON path")
    args = ap.parse_args()
    if not args.bundle.exists():
        raise SystemExit(f"{args.bundle} does not exist.")

    records = translate(args.bundle)
    with args.out.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    print(f"Translated {len(records)} paired stages → {args.out}")


if __name__ == "__main__":
    main()
