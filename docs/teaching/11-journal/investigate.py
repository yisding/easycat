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
    uv run python docs/teaching/11-journal/investigate.py \\
        bundles/bug_03_ghost_interruption.bundle --turn ch11-bug03-turn-2 \\
        --include-session-context

Dependencies:
    uv sync --group dev
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from easycat.debug.testing import load_bundle


def _record_matches(
    record,
    *,
    stage=None,
    turn=None,
    sequence=None,
    name=None,
    session_context_ids: frozenset[str] = frozenset(),
) -> bool:
    data = record.get("data") or {}
    turn_matches = (
        turn is None
        or record.get("turn_id") == turn
        or (record.get("turn_id") is None and record.get("session_id") in session_context_ids)
    )
    return (
        (stage is None or data.get("stage") == stage or data.get("observed_stage") == stage)
        and turn_matches
        and (sequence is None or record.get("sequence") == sequence)
        and (name is None or record.get("name") == name)
    )


def query_records(
    bundle,
    *,
    stage=None,
    turn=None,
    sequence=None,
    name=None,
    include_session_context: bool = False,
):
    """Query a ``RunBundle`` through its public read-only helpers.

    Start with the most selective public operation, then apply any
    remaining filters. The opt-in session-context join scans the bundle
    because public turn filtering intentionally excludes unscoped records.
    ``RunBundle`` records are dictionaries; a live ``JournalView`` offers
    the same three helper names but returns typed ``JournalRecord`` objects.
    """
    turn_records = bundle.filter_by_turn(turn) if turn is not None else []
    session_context_ids = (
        frozenset(record.get("session_id") for record in turn_records)
        if include_session_context
        else frozenset()
    )
    if sequence is not None:
        record = bundle.lookup_by_sequence(sequence)
        records = [] if record is None else [record]
    elif turn is not None:
        records = (
            [
                record
                for record in bundle.records()
                if record.get("turn_id") == turn
                or (
                    record.get("turn_id") is None
                    and record.get("session_id") in session_context_ids
                )
            ]
            if include_session_context
            else turn_records
        )
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
            session_context_ids=session_context_ids,
        )
    ]


def query_diagnostics(
    records,
    *,
    stage=None,
    turn=None,
    sequence=None,
    name=None,
    include_session_context: bool = False,
) -> dict:
    """Describe filter coverage without changing the query result."""
    session_context_ids = (
        frozenset(
            record.get("session_id")
            for record in records
            if turn is not None and record.get("turn_id") == turn
        )
        if include_session_context
        else frozenset()
    )
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
        key: sum(
            _record_matches(
                record,
                **{key: value},
                session_context_ids=(session_context_ids if key == "turn" else frozenset()),
            )
            for record in records
        )
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
        "include_session_context": include_session_context,
        "marginal_matches": marginal_matches,
        "session_context_matches": sum(
            record.get("turn_id") is None and record.get("session_id") in session_context_ids
            for record in records
        ),
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


def print_query_result(records, diagnostics: dict, *, limit: int) -> None:
    """Render matches plus enough coverage to interpret an empty result."""
    active = diagnostics["filters"]
    filter_text = ", ".join(f"{key}={value!r}" for key, value in active.items()) or "none"
    print(f"  filters: {filter_text}")
    print(f"  matched: {len(records)} of {diagnostics['total_records']} records")
    if diagnostics["include_session_context"]:
        included_context = sum(record.get("turn_id") is None for record in records)
        print(
            f"  session context: {included_context} unscoped records included from target session"
        )

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
    ap.add_argument(
        "--include-session-context",
        action="store_true",
        help="With --turn, also include turn-less records from that turn's session.",
    )
    ap.add_argument("--limit", type=positive_int, default=80)
    ap.add_argument(
        "--require-match",
        action="store_true",
        help="Exit non-zero when the composed query matches no records.",
    )
    args = ap.parse_args()
    if args.include_session_context and args.turn is None:
        ap.error("--include-session-context requires --turn")

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
        include_session_context=args.include_session_context,
    )
    diagnostics = query_diagnostics(
        all_records,
        stage=args.stage,
        turn=args.turn,
        sequence=args.sequence,
        name=args.name,
        include_session_context=args.include_session_context,
    )
    print_query_result(records, diagnostics, limit=args.limit)
    if not records and args.require_match:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
