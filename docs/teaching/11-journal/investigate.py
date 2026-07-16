"""Chapter 11 — inspect a planted-bug bundle.

Use this script to query the three bundles in ``bundles/``. It's a
tiny harness; the *investigation* happens in your head.

    uv run python docs/teaching/11-journal/investigate.py \\
        bundles/bug_01_empty_final.bundle
    uv run python docs/teaching/11-journal/investigate.py \\
        bundles/bug_02_tts_stutter.bundle --stage tts
    uv run python docs/teaching/11-journal/investigate.py \\
        bundles/bug_03_ghost_interruption.bundle --name interruption.start
    uv run python docs/teaching/11-journal/investigate.py \\
        bundles/bug_03_ghost_interruption.bundle --turn ch11-bug03-turn-2
    uv run python docs/teaching/11-journal/investigate.py \\
        bundles/bug_03_ghost_interruption.bundle --sequence 9

Dependencies:
    uv sync --group dev
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from easycat.debug.bundle import RunBundle
from easycat.debug.testing import load_bundle


def query_records(
    bundle: RunBundle,
    *,
    stage: str | None = None,
    turn: str | None = None,
    sequence: int | None = None,
    name: str | None = None,
) -> list[dict[str, Any]]:
    """Query a ``RunBundle`` through its public read-only helpers.

    Start with the most selective public operation, then apply any
    remaining filters. ``RunBundle`` records are dictionaries; a live
    ``JournalView`` offers the same three helper names but returns typed
    ``JournalRecord`` objects instead.
    """
    if sequence is not None:
        record = bundle.lookup_by_sequence(sequence)
        records = [] if record is None else [record]
    elif turn is not None:
        records = bundle.filter_by_turn(turn)
    elif stage is not None:
        records = bundle.filter_by_stage(stage)
    else:
        records = list(bundle.records())

    return [
        record
        for record in records
        if (
            stage is None
            or (record.get("data") or {}).get("stage") == stage
            or (record.get("data") or {}).get("observed_stage") == stage
        )
        and (turn is None or record.get("turn_id") == turn)
        and (name is None or record.get("name") == name)
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--stage", help="Filter to records where data.stage == STAGE.")
    ap.add_argument("--turn", help="Filter to records with turn_id == TURN.")
    ap.add_argument("--sequence", type=int, help="Look up one exact journal sequence.")
    ap.add_argument("--name", help="Filter to records with name == NAME.")
    ap.add_argument("--limit", type=int, default=80)
    args = ap.parse_args()

    if not args.bundle.exists():
        sys.exit(
            f"{args.bundle} does not exist. Run generate_bundles.py to (re)build the fixtures."
        )

    bundle = load_bundle(args.bundle)
    print(f"=== {args.bundle.name} ===")

    # ``RunBundle`` ships ``filter_by_stage``, ``filter_by_turn``, and
    # ``lookup_by_sequence``
    # mirroring ``JournalView``'s API. When you get to a live session,
    # ``session.journal`` is a ``JournalView`` with the same surface —
    # the query vocabulary transfers, while the record representation differs.
    records = query_records(
        bundle,
        stage=args.stage,
        turn=args.turn,
        sequence=args.sequence,
        name=args.name,
    )

    count = 0
    for r in records:
        data = r.get("data") or {}
        seq = r.get("sequence")
        name = r.get("name")
        turn = r.get("turn_id") or "-"
        print(f"  #{seq:>3}  turn={turn:22}  {name:30}  {data}")
        count += 1
        if count >= args.limit:
            print(f"  ... (stopped at --limit {args.limit})")
            break
    if count == 0:
        print("  (no records matched)")


if __name__ == "__main__":
    main()
