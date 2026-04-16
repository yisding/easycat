"""Shared assertion helpers for E2E tests."""

from __future__ import annotations

from collections.abc import Iterable

from easycat.runtime import JournalRecordKind


def journal_event_names(journal) -> list[str]:  # type: ignore[no-untyped-def]
    """Return the ordered list of EVENT-kind record names in the journal."""
    return [r.name for r in journal.slice(kind=JournalRecordKind.EVENT)]


def assert_turn_complete(journal, turn_id: str | None = None) -> None:  # type: ignore[no-untyped-def]
    """Assert the canonical turn-lifecycle events are present.

    In the voice pipeline the user's ``turn_ended`` fires when VAD
    detects end-of-speech; the agent response and ``bot_*_speaking``
    events follow afterward. So we just check for event presence, not
    strict ordering within the user-turn window.
    """
    events = journal.slice(kind=JournalRecordKind.EVENT)
    if turn_id is not None:
        events = [e for e in events if e.turn_id == turn_id]
    names = [e.name for e in events]
    for r in ("turn_started", "turn_ended"):
        assert r in names, f"missing {r!r} in journal names={names}"
    # turn_started must come before turn_ended overall
    assert names.index("turn_started") < names.index("turn_ended"), (
        f"turn_started / turn_ended out of order: {names}"
    )


def assert_no_dangling_artifacts(journal, artifact_store) -> None:  # type: ignore[no-untyped-def]
    """Every ref in the journal must resolve in the store."""
    records = journal.read()
    refs: set[str] = set()
    for r in records:
        if r.input_ref:
            refs.add(r.input_ref)
        if r.output_ref:
            refs.add(r.output_ref)
    missing = [ref for ref in refs if not artifact_store.has(ref)]
    assert not missing, f"dangling artifact refs: {missing}"


def assert_strictly_monotonic_sequences(records: Iterable) -> None:  # type: ignore[no-untyped-def]
    seqs = [r.sequence for r in records]
    assert seqs == sorted(seqs), f"sequences out of order: {seqs}"
    assert len(set(seqs)) == len(seqs), f"duplicate sequences: {seqs}"


def count_distinct_turns(journal) -> int:  # type: ignore[no-untyped-def]
    return len({r.turn_id for r in journal.read() if r.turn_id})


__all__ = [
    "assert_no_dangling_artifacts",
    "assert_strictly_monotonic_sequences",
    "assert_turn_complete",
    "count_distinct_turns",
    "journal_event_names",
]
