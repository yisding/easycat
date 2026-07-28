"""Bounded in-memory journal backend (the default ``debug="light"`` backend)."""

from __future__ import annotations

import collections
import copy
import logging
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from easycat._observability import observe_gauge, record_histogram
from easycat.runtime._journal_codec import _journal_record_for_append
from easycat.runtime.journal import _read_records, _slice_records
from easycat.runtime.journal_views import FrozenJournalSnapshot
from easycat.runtime.records import (
    BufferOverflow,
    ErrorInfo,
    JournalDegraded,
    JournalRecord,
    JournalRecordKind,
    TimingInfo,
)

if TYPE_CHECKING:
    from easycat.runtime.artifacts import InMemoryArtifactStore

logger = logging.getLogger(__name__)


# ── InMemoryRingBuffer backend ───────────────────────────────────


class InMemoryRingBuffer:
    """Bounded in-memory journal backend.

    Safe for concurrent sync writes (``threading.Lock``).  Drops the oldest
    record when capacity is exceeded and emits a ``BufferOverflow`` marker.
    """

    def __init__(
        self,
        capacity: int = 10_000,
        artifact_store: InMemoryArtifactStore | None = None,
    ) -> None:
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        self._capacity = capacity
        self._buf: collections.deque[JournalRecord] = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0
        self._degraded = False
        logger.debug("In-memory journal: crash-durability waived (data lost on process exit)")
        self._dropped_records = 0
        self._artifact_store = artifact_store
        self._ref_counts: dict[str, int] = {}  # ref → number of records referencing it

    # ── ExecutionJournal interface ────────────────────────────────

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
        started = time.perf_counter()
        result = "fail"
        if self._degraded:
            record_histogram(
                "easycat.journal.append.latency",
                time.perf_counter() - started,
                {"easycat.result": result},
            )
            return -1
        try:
            sequence = self._do_append(
                kind,
                name,
                session_id,
                turn_id,
                data,
                error,
                tags,
                input_ref,
                output_ref,
            )
            result = "pass"
            return sequence
        except Exception as exc:
            self._enter_degraded(session_id, exc)
            return -1
        finally:
            record_histogram(
                "easycat.journal.append.latency",
                time.perf_counter() - started,
                {"easycat.result": result},
            )

    def read(self, start: int = 0, limit: int | None = None) -> list[JournalRecord]:
        with self._lock:
            records = list(self._buf)
        return copy.deepcopy(_read_records(records, start=start, limit=limit))

    def slice(
        self,
        *,
        kind: JournalRecordKind | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        name: str | None = None,
        tags: frozenset[str] | None = None,
    ) -> list[JournalRecord]:
        with self._lock:
            records = list(self._buf)
        return copy.deepcopy(
            _slice_records(
                records,
                kind=kind,
                session_id=session_id,
                turn_id=turn_id,
                name=name,
                tags=tags,
            )
        )

    def close(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def finalize(self) -> None:
        pass

    def snapshot(self) -> FrozenJournalSnapshot:
        """Return a read-only copy of the current buffer contents."""
        with self._lock:
            return FrozenJournalSnapshot(
                copy.deepcopy(list(self._buf)),
                degraded=self._degraded,
                latest_sequence=self._seq,
                dropped_records=self._dropped_records,
            )

    @property
    def latest_sequence(self) -> int:
        with self._lock:
            return self._seq

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def dropped_records(self) -> int:
        with self._lock:
            return self._dropped_records

    # ── Internals ─────────────────────────────────────────────────

    def _do_append(
        self,
        kind: JournalRecordKind,
        name: str,
        session_id: str,
        turn_id: str | None,
        data: dict[str, Any] | None,
        error: ErrorInfo | None,
        tags: frozenset[str],
        input_ref: str | None = None,
        output_ref: str | None = None,
    ) -> int:
        now_timing = TimingInfo(
            wall_ns=time.time_ns(),
            mono_ns=time.monotonic_ns(),
            cpu_ns=time.process_time_ns(),
        )
        with self._lock:
            was_full = len(self._buf) == self._capacity

            # Collect artifact refs from the record about to be evicted.
            evicted_refs = self._refs_of_next_eviction() if was_full else []

            self._seq += 1
            seq = self._seq
            record = _journal_record_for_append(
                sequence=seq,
                session_id=session_id,
                kind=kind,
                name=name,
                timing=now_timing,
                turn_id=turn_id,
                data=data or {},
                error=error,
                input_ref=input_ref,
                output_ref=output_ref,
                tags=tags,
            )
            self._buf.append(record)

            # Track ref counts for the new record.
            if input_ref:
                self._ref_counts[input_ref] = self._ref_counts.get(input_ref, 0) + 1
            if output_ref:
                self._ref_counts[output_ref] = self._ref_counts.get(output_ref, 0) + 1

            # Decrement ref counts for evicted record and clean up orphans.
            if was_full:
                self._dropped_records += 1
                self._decrement_and_evict_refs(evicted_refs)

            if was_full:
                self._upsert_overflow_marker(session_id, now_timing)
        return seq

    def _upsert_overflow_marker(self, session_id: str, timing: TimingInfo) -> None:
        """Keep one current loss marker in the bounded buffer. Caller holds lock."""
        for index, record in enumerate(self._buf):
            if record.name == "buffer_overflow":
                self._buf[index] = replace(
                    record,
                    data={
                        "dropped_from": "ring_buffer",
                        "dropped_records": self._dropped_records,
                    },
                )
                return

        # The prior marker was itself evicted. Reinsert it and account for the
        # additional record displaced to make the loss signal visible.
        evicted_refs = self._refs_of_next_eviction() if len(self._buf) == self._capacity else []
        if len(self._buf) == self._capacity:
            self._dropped_records += 1
        self._seq += 1
        self._buf.append(
            BufferOverflow(
                sequence=self._seq,
                session_id=session_id,
                timing=timing,
                data={
                    "dropped_from": "ring_buffer",
                    "dropped_records": self._dropped_records,
                },
            )
        )
        if evicted_refs:
            self._decrement_and_evict_refs(evicted_refs)

    def _refs_of_next_eviction(self) -> list[str]:
        """Artifact refs held by the record about to be evicted. Caller holds lock."""
        refs: list[str] = []
        if self._buf:
            evicted = self._buf[0]
            if evicted.input_ref:
                refs.append(evicted.input_ref)
            if evicted.output_ref:
                refs.append(evicted.output_ref)
        return refs

    def _decrement_and_evict_refs(self, refs: list[str]) -> None:
        """Decrement ref counts and delete orphaned artifacts. Caller holds lock."""
        if not self._artifact_store:
            return
        for ref in refs:
            count = self._ref_counts.get(ref, 0) - 1
            if count <= 0:
                self._ref_counts.pop(ref, None)
                self._artifact_store.delete(ref)
            else:
                self._ref_counts[ref] = count

    def _enter_degraded(self, session_id: str, exc: Exception) -> None:
        self._degraded = True
        observe_gauge("easycat.journal.degraded", 1)
        # Build the marker once with sequence=-1 so it does not consume a live
        # sequence number.  ``append`` returns -1 in degraded mode, and keeping
        # the marker at -1 means ``latest_sequence`` does not advance past a
        # sequence that no ``append`` return value corresponds to.
        #
        # The -1 marker is a deliberate *out-of-band* signal.  Because
        # ``read(start)`` filters ``sequence >= start`` and ``follow()`` always
        # uses a cursor ``>= 0``, this marker is intentionally NOT delivered
        # through the normal ``read()``/``follow()`` record stream.  Consumers
        # MUST consult the ``degraded`` property (kept in sync below and on
        # ``JournalView``) to detect degradation, not scan the record stream.
        # The marker is still recoverable for forensic inspection via
        # ``read(start=-1)`` / ``slice()``.
        marker = JournalDegraded(
            sequence=-1,
            session_id=session_id,
            timing=TimingInfo(
                wall_ns=time.time_ns(),
                mono_ns=time.monotonic_ns(),
                cpu_ns=time.process_time_ns(),
            ),
            data={
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
        )
        logger.warning("Journal entered degraded mode: %s: %s", type(exc).__name__, exc)
        # Try to write the marker — best-effort.
        try:
            with self._lock:
                was_full = len(self._buf) == self._capacity
                evicted_refs = self._refs_of_next_eviction() if was_full else []
                self._buf.append(marker)
                if was_full:
                    self._dropped_records += 1
                    self._decrement_and_evict_refs(evicted_refs)
        except Exception:
            pass
