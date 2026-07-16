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


def _record_matches(
    record: dict[str, Any],
    *,
    stage: str | None = None,
    turn: str | None = None,
    sequence: int | None = None,
    name: str | None = None,
) -> bool:
    data = record.get("data") or {}
    return (
        (stage is None or data.get("stage") == stage or data.get("observed_stage") == stage)
        and (turn is None or record.get("turn_id") == turn)
        and (sequence is None or record.get("sequence") == sequence)
        and (name is None or record.get("name") == name)
    )


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
        if _record_matches(
            record,
            stage=stage,
            turn=turn,
            sequence=sequence,
            name=name,
        )
    ]


def query_diagnostics(
    records: list[dict[str, Any]],
    *,
    stage: str | None = None,
    turn: str | None = None,
    sequence: int | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Describe filter coverage without changing the query result."""
    filters = {
        key: value
        for key, value in {
            "stage": stage,
            "turn": turn,
            "sequence": sequence,
            "name": name,
        }.items()
        if value is not None
    }
    marginal_matches = {
        key: sum(_record_matches(record, **{key: value}) for record in records)
        for key, value in filters.items()
    }
    stages = sorted(
        {
            stage_name
            for record in records
            for stage_name in (
                (record.get("data") or {}).get("stage"),
                (record.get("data") or {}).get("observed_stage"),
            )
            if stage_name
        }
    )
    sequences = [record.get("sequence") for record in records]
    numeric_sequences = [value for value in sequences if isinstance(value, int)]
    return {
        "filters": filters,
        "marginal_matches": marginal_matches,
        "known_turns": sorted(
            {record.get("turn_id") for record in records if record.get("turn_id")}
        ),
        "known_stages": stages,
        "known_names": sorted({record.get("name") for record in records if record.get("name")}),
        "sequence_range": (
            [min(numeric_sequences), max(numeric_sequences)] if numeric_sequences else None
        ),
        "total_records": len(records),
    }


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def print_query_result(
    records: list[dict[str, Any]], diagnostics: dict[str, Any], *, limit: int
) -> None:
    """Render matches plus enough coverage to interpret an empty result."""
    active = diagnostics["filters"]
    filter_text = ", ".join(f"{key}={value!r}" for key, value in active.items()) or "none"
    print(f"  filters: {filter_text}")
    print(f"  matched: {len(records)} of {diagnostics['total_records']} records")

    for record in records[:limit]:
        data = record.get("data") or {}
        seq = record.get("sequence")
        name = record.get("name")
        turn = record.get("turn_id") or "-"
        print(f"  #{seq:>3}  turn={turn:22}  {name:30}  {data}")
    if len(records) > limit:
        print(f"  ... (showing {limit} of {len(records)} matches)")
    if records:
        return

    print("  (no records matched)")
    for key, count in diagnostics["marginal_matches"].items():
        print(f"  marginal {key}: {count} matches")
    for key, inventory_key in (
        ("turn", "known_turns"),
        ("stage", "known_stages"),
        ("name", "known_names"),
    ):
        if key in active:
            print(f"  known {key}s: {diagnostics[inventory_key]}")
    if "sequence" in active:
        print(f"  sequence range: {diagnostics['sequence_range']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--stage", help="Filter to records where data.stage == STAGE.")
    ap.add_argument("--turn", help="Filter to records with turn_id == TURN.")
    ap.add_argument("--sequence", type=int, help="Look up one exact journal sequence.")
    ap.add_argument("--name", help="Filter to records with name == NAME.")
    ap.add_argument("--limit", type=positive_int, default=80)
    ap.add_argument(
        "--require-match",
        action="store_true",
        help="Exit non-zero when the composed query matches no records.",
    )
    args = ap.parse_args()

    if not args.bundle.exists():
        sys.exit(
            f"{args.bundle} does not exist. Run generate_bundles.py to (re)build the fixtures."
        )

    bundle = load_bundle(args.bundle)
    print(f"=== {args.bundle.name} ===")
    all_records = list(bundle.records())

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
    diagnostics = query_diagnostics(
        all_records,
        stage=args.stage,
        turn=args.turn,
        sequence=args.sequence,
        name=args.name,
    )
    print_query_result(records, diagnostics, limit=args.limit)
    if not records and args.require_match:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
