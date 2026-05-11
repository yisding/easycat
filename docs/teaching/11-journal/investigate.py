"""Chapter 11 — inspect a planted-bug bundle.

Use this script to query the three bundles in ``bundles/``. It's a
tiny harness; the *investigation* happens in your head.

    uv run python docs/teaching/11-journal/investigate.py \\
        bundles/bug_01_empty_final.bundle
    uv run python docs/teaching/11-journal/investigate.py \\
        bundles/bug_02_tts_stutter.bundle --stage tts
    uv run python docs/teaching/11-journal/investigate.py \\
        bundles/bug_03_ghost_interruption.bundle --name interruption.start

Dependencies:
    uv sync --group dev
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from easycat.debug.testing import load_bundle


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--stage", help="Filter to records where data.stage == STAGE.")
    ap.add_argument("--name", help="Filter to records with name == NAME.")
    ap.add_argument("--limit", type=int, default=80)
    args = ap.parse_args()

    if not args.bundle.exists():
        sys.exit(
            f"{args.bundle} does not exist. Run generate_bundles.py to (re)build the fixtures."
        )

    bundle = load_bundle(args.bundle)
    print(f"=== {args.bundle.name} ===")

    # ``RunBundle`` ships ``filter_by_stage`` and ``lookup_by_sequence``
    # mirroring ``JournalView``'s API. When you get to a live session,
    # ``session.journal`` is a ``JournalView`` with the same surface —
    # the query you learn here works on both.
    if args.stage and not args.name:
        records = bundle.filter_by_stage(args.stage)
    else:
        records = list(bundle.records())

    count = 0
    for r in records:
        data = r.get("data") or {}
        if args.stage and data.get("stage") != args.stage:
            continue
        if args.name and r.get("name") != args.name:
            continue
        seq = r.get("sequence")
        name = r.get("name")
        print(f"  #{seq:>3}  {name:30}  {data}")
        count += 1
        if count >= args.limit:
            print(f"  ... (stopped at --limit {args.limit})")
            break
    if count == 0:
        print("  (no records matched)")


if __name__ == "__main__":
    main()
