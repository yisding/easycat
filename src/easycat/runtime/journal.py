"""ExecutionJournal protocol and the read-only JournalView query surface.

Backend implementations live in sibling modules:

- :mod:`easycat.runtime.journal_memory` — ``InMemoryRingBuffer``
- :mod:`easycat.runtime.journal_sql` — ``SqliteJournal`` / ``LitestreamSqliteJournal`` /
  ``LibsqlJournal``
- :mod:`easycat.runtime.journal_views` — ``ReadonlySqliteJournal`` /
  ``FrozenJournalSnapshot``
- :mod:`easycat.runtime.journal_retention` — ``run_retention``
- :mod:`easycat.runtime.journal_factory` — ``create_journal``
"""

from __future__ import annotations

import asyncio
import collections
from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable

from easycat.runtime.records import (
    BufferOverflow,
    ErrorInfo,
    JournalRecord,
    JournalRecordKind,
)

__all__ = [
    "ExecutionJournal",
    "JournalView",
    "append_journal_record_async",
]


def _validate_read_limit(limit: int | None) -> None:
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")


def _read_records(
    records: Iterable[JournalRecord],
    *,
    start: int = 0,
    limit: int | None = None,
) -> list[JournalRecord]:
    """Apply the in-memory journal read contract to a record snapshot."""
    _validate_read_limit(limit)
    out = [record for record in records if record.sequence >= start]
    if limit is not None:
        out = out[:limit]
    return out


def _slice_records(
    records: Iterable[JournalRecord],
    *,
    kind: JournalRecordKind | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    name: str | None = None,
    tags: frozenset[str] | None = None,
) -> list[JournalRecord]:
    """Apply the in-memory journal filter contract to a record snapshot."""
    out = list(records)
    if kind is not None:
        out = [record for record in out if record.kind == kind]
    if session_id is not None:
        out = [record for record in out if record.session_id == session_id]
    if turn_id is not None:
        out = [record for record in out if record.turn_id == turn_id]
    if name is not None:
        out = [record for record in out if record.name == name]
    if tags:
        out = [record for record in out if tags <= record.tags]
    return out


# ── Protocol ──────────────────────────────────────────────────────


@runtime_checkable
class ExecutionJournal(Protocol):
    """Append-only structured journal for session records."""

    def append(
        self,
        kind: JournalRecordKind,
        name: str,
        session_id: str,
        turn_id: str | None = None,
        data: dict[str, Any] | None = None,
        error: ErrorInfo | None = None,
        tags: frozenset[str] = frozenset(),
        input_ref: str | None = None,
        output_ref: str | None = None,
    ) -> int:
        """Append a record. Returns the assigned sequence number.

        *input_ref* / *output_ref* are stable artifact-store refs (SHA-256
        hex).  The caller must ensure the referenced artifact has been
        committed **before** calling ``append`` — this is the atomicity
        contract that guarantees no durable record carries a dangling ref.

        Must never raise — failures trigger degraded mode.
        """
        ...

    def read(self, start: int = 0, limit: int | None = None) -> list[JournalRecord]:
        """Return records with ``sequence >= start``, up to *limit*."""
        ...

    def slice(
        self,
        *,
        kind: JournalRecordKind | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        name: str | None = None,
        tags: frozenset[str] | None = None,
    ) -> list[JournalRecord]:
        """Return records matching the given filters.

        *kind*/*session_id*/*turn_id*/*name* are exact-match (indexed on the
        SQL backends; ``turn_id`` is backed by ``idx_journal_turn_id``).
        *tags* is a **subset** match — a record matches when every requested
        tag is present in its tag set.  The live SQL backends resolve tags
        through the indexed ``journal_tags`` junction table; the in-memory
        backends do an exact subset test.
        """
        ...

    def close(self) -> None: ...

    def flush(self) -> None: ...

    def finalize(self) -> None:
        """Mark the session as cleanly closed without closing the backend.

        Writes the ``clean_close`` marker (for backends that support it)
        so that a subsequent session with the same id is not treated as
        crash recovery.  The backend remains readable — callers can still
        query records after this call.  ``close()`` is still required to
        release the underlying connection.
        """
        ...

    @property
    def latest_sequence(self) -> int: ...

    @property
    def degraded(self) -> bool: ...


async def append_journal_record_async(
    journal: ExecutionJournal,
    *,
    kind: JournalRecordKind,
    name: str,
    session_id: str,
    turn_id: str | None = None,
    data: dict[str, Any] | None = None,
    error: ErrorInfo | None = None,
    tags: frozenset[str] = frozenset(),
    input_ref: str | None = None,
    output_ref: str | None = None,
) -> int:
    """Append without blocking the event loop for disk-backed journals.

    Persistent/custom backends declare ``writes_block = True`` when their
    synchronous ``append`` path can cross a syscall boundary. Those writes
    run in the loop's worker pool; in-memory journals stay inline because a
    thread hop costs more than their lock-and-deque append.
    """

    def _append() -> int:
        return journal.append(
            kind=kind,
            name=name,
            session_id=session_id,
            turn_id=turn_id,
            data=data,
            error=error,
            tags=tags,
            input_ref=input_ref,
            output_ref=output_ref,
        )

    if bool(getattr(journal, "writes_block", False)):
        worker = asyncio.create_task(asyncio.to_thread(_append))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Cancelling an asyncio wrapper cannot stop a synchronous worker.
            # Keep ownership until the append is finished so teardown cannot
            # close the backend while an untracked write is still in flight.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
            try:
                worker.result()
            except BaseException:  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
                # Cancellation remains the caller-visible outcome; retrieving
                # the result prevents a detached worker exception warning.
                pass
            raise
    return _append()


# ── JournalView (read-only surface) ──────────────────────────────


class JournalView:
    """Read-only view exposed as ``Session.journal``."""

    def __init__(self, journal: ExecutionJournal) -> None:
        self._journal = journal

    def read(self, start: int = 0, limit: int | None = None) -> list[JournalRecord]:
        _validate_read_limit(limit)
        return self._journal.read(start=start, limit=limit)

    def slice(
        self,
        *,
        kind: JournalRecordKind | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        name: str | None = None,
        tags: frozenset[str] | None = None,
    ) -> list[JournalRecord]:
        return self._journal.slice(
            kind=kind,
            session_id=session_id,
            turn_id=turn_id,
            name=name,
            tags=tags,
        )

    def filter_by_stage(self, stage_name: str) -> list[JournalRecord]:
        """Return records whose ``data['stage']`` or ``data['observed_stage']``
        matches *stage_name*.  Mirrors :meth:`RunBundle.filter_by_stage`.

        The live SQL backends persist derived ``stage`` and ``observed_stage``
        columns with indexes and expose ``slice_by_stage`` for an index lookup,
        so this no longer deserializes every record on those backends.
        Backends without the indexed column (read-only views over older files,
        frozen in-memory snapshots) fall back to a scan — correct everywhere,
        fast where the index exists.
        """
        indexed = getattr(self._journal, "slice_by_stage", None)
        if indexed is not None:
            return indexed(stage_name)
        results: list[JournalRecord] = []
        for r in self._journal.read():
            stage = r.data.get("stage")
            observed = r.data.get("observed_stage")
            if stage == stage_name or observed == stage_name:
                results.append(r)
        return results

    def filter_by_turn(self, turn_id: str) -> list[JournalRecord]:
        """Return records whose ``turn_id`` matches.  Mirrors
        :meth:`RunBundle.filter_by_turn`.

        Delegates to :meth:`slice`, so on the SQL backends this is an indexed
        ``WHERE turn_id = ?`` lookup (``idx_journal_turn_id``) rather than a
        deserialize-every-record scan."""
        return self._journal.slice(turn_id=turn_id)

    def lookup_by_sequence(self, seq: int) -> JournalRecord | None:
        """Return the record with the given sequence number, or ``None``.
        Mirrors :meth:`RunBundle.lookup_by_sequence`.

        ``sequence`` is the primary key, so this is a bounded ``read(start, 1)``
        lookup rather than a full-table scan."""
        recs = self._journal.read(start=seq, limit=1)
        return recs[0] if recs and recs[0].sequence == seq else None

    async def follow(  # noqa: C901
        self,
        *,
        from_sequence: int | None = None,
        poll_interval: float = 0.05,
        stop: asyncio.Event | None = None,
    ) -> collections.abc.AsyncIterator[JournalRecord]:
        """Yield new records as they are appended.

        *from_sequence* sets the starting cursor.  ``None`` (default) means
        start after the current ``latest_sequence`` — i.e. only future records.
        Pass ``0`` to replay the full history then live-tail.

        Polls ``latest_sequence`` on *poll_interval* seconds.

        **Lossiness on bounded buffers:** with the in-memory ring buffer,
        records can be evicted (``deque`` ``maxlen``) before this loop observes
        them if appends outpace *poll_interval*.  When the cursor falls behind
        the oldest retained record, ``follow`` yields a synthetic
        :class:`BufferOverflow` notice (``data['dropped_from'] == 'follow_gap'``
        with ``data['gap']`` = number of skipped sequences) so the consumer has
        an in-band signal that the sequence stream is non-contiguous.  Persistent
        backends (SQLite/libSQL) retain every record, so no gap occurs there.

        **Stopping:** the loop exits when *stop* (an :class:`asyncio.Event`) is
        set, or when the caller closes the generator (``aclose()`` / breaking
        out of ``async for``), which raises ``GeneratorExit`` at the next yield.
        """
        if from_sequence is not None:
            cursor = from_sequence
        else:
            # Read latest_sequence and compute cursor atomically — the
            # property getter holds the backend lock, so no record can
            # slip in between read and +1.
            cursor = self._journal.latest_sequence + 1
        while True:
            if stop is not None and stop.is_set():
                return
            # Fetch records from cursor onward.  read() is lock-protected
            # in every backend, so we won't miss records that were appended
            # between the previous iteration's yield and this call.
            records = self._journal.read(start=cursor)
            # Real sequences start at 1 (the ring buffer's ``_seq`` and the
            # SQLite counter both pre-increment from 0).  ``from_sequence=0`` is
            # the documented "replay full history then live-tail" cursor — it
            # points below the first real sequence, so first record at 1 is not
            # a gap, but any first record >1 means history was evicted (gh 1046).
            if records and records[0].sequence > cursor:
                if cursor == 0:
                    if records[0].sequence > 1:
                        gap = records[0].sequence - 1
                        yield BufferOverflow(
                            sequence=1,
                            session_id=records[0].session_id,
                            timing=records[0].timing,
                            data={"dropped_from": "follow_gap", "gap": gap},
                        )
                elif records[0].sequence > cursor:
                    gap = records[0].sequence - cursor
                    yield BufferOverflow(
                        sequence=cursor,
                        session_id=records[0].session_id,
                        timing=records[0].timing,
                        data={"dropped_from": "follow_gap", "gap": gap},
                    )
            for rec in records:
                yield rec
                # Advance cursor past the yielded record so we never
                # re-deliver it, even if the caller suspends mid-batch.
                cursor = rec.sequence + 1
            if stop is not None and stop.is_set():
                return
            if stop is not None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll_interval)
                    return
                except TimeoutError:
                    continue
            await asyncio.sleep(poll_interval)

    @property
    def enabled(self) -> bool:
        return True

    @property
    def latest_sequence(self) -> int:
        """The highest sequence number appended so far (``0`` when empty).

        Re-exposes the backend's O(1) counter so callers can cheaply detect
        journal growth (e.g. a live-tail loop gating on append) without
        re-reading or re-serializing the whole journal.
        """
        return self._journal.latest_sequence

    @property
    def degraded(self) -> bool:
        return self._journal.degraded

    @property
    def dropped_records(self) -> int:
        """Number of records evicted by a bounded backend (zero otherwise)."""
        value = getattr(self._journal, "dropped_records", 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0
