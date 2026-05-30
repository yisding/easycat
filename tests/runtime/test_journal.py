"""Tests for ExecutionJournal, InMemoryRingBuffer, and JournalView."""

from __future__ import annotations

import asyncio
import logging
import threading

from easycat.runtime.journal import InMemoryRingBuffer, JournalView, create_journal
from easycat.runtime.records import JournalRecordKind


class TestInMemoryRingBuffer:
    def test_append_and_read(self):
        j = InMemoryRingBuffer(capacity=100)
        seq = j.append(
            kind=JournalRecordKind.EVENT,
            name="STTFinal",
            session_id="s1",
            data={"text": "hello"},
        )
        assert seq == 1
        records = j.read()
        assert len(records) == 1
        assert records[0].sequence == 1
        assert records[0].name == "STTFinal"
        assert records[0].data["text"] == "hello"

    def test_monotonic_sequence(self):
        j = InMemoryRingBuffer(capacity=1000)
        seqs = []
        for i in range(100):
            seq = j.append(
                kind=JournalRecordKind.EVENT,
                name=f"event_{i}",
                session_id="s1",
            )
            seqs.append(seq)
        assert seqs == list(range(1, 101))
        assert j.latest_sequence == 100

    def test_read_with_start(self):
        j = InMemoryRingBuffer(capacity=100)
        for i in range(10):
            j.append(kind=JournalRecordKind.EVENT, name=f"e{i}", session_id="s1")
        records = j.read(start=6)
        assert len(records) == 5
        assert records[0].sequence == 6

    def test_read_with_limit(self):
        j = InMemoryRingBuffer(capacity=100)
        for i in range(10):
            j.append(kind=JournalRecordKind.EVENT, name=f"e{i}", session_id="s1")
        records = j.read(start=1, limit=3)
        assert len(records) == 3

    def test_slice_by_kind(self):
        j = InMemoryRingBuffer(capacity=100)
        j.append(kind=JournalRecordKind.EVENT, name="e1", session_id="s1")
        j.append(kind=JournalRecordKind.METRIC, name="m1", session_id="s1")
        j.append(kind=JournalRecordKind.EVENT, name="e2", session_id="s1")
        events = j.slice(kind=JournalRecordKind.EVENT)
        assert len(events) == 2
        metrics = j.slice(kind=JournalRecordKind.METRIC)
        assert len(metrics) == 1

    def test_slice_by_session_id(self):
        j = InMemoryRingBuffer(capacity=100)
        j.append(kind=JournalRecordKind.EVENT, name="e1", session_id="s1")
        j.append(kind=JournalRecordKind.EVENT, name="e2", session_id="s2")
        assert len(j.slice(session_id="s1")) == 1
        assert len(j.slice(session_id="s2")) == 1

    def test_overflow_drops_oldest(self):
        j = InMemoryRingBuffer(capacity=5)
        for i in range(10):
            j.append(kind=JournalRecordKind.EVENT, name=f"e{i}", session_id="s1")
        records = j.read()
        # Capacity 5, after 10 appends + overflow markers, oldest are dropped.
        # The deque maxlen governs how many records survive.
        assert len(records) <= 5
        # All surviving records should have sequences > 0
        assert all(r.sequence > 0 for r in records)

    def test_overflow_emits_marker(self):
        j = InMemoryRingBuffer(capacity=3)
        for i in range(5):
            j.append(kind=JournalRecordKind.EVENT, name=f"e{i}", session_id="s1")
        records = j.read()
        overflow_records = [r for r in records if r.kind == JournalRecordKind.CONTROL]
        assert len(overflow_records) >= 1
        assert overflow_records[0].name == "buffer_overflow"

    def test_close_is_noop(self):
        j = InMemoryRingBuffer(capacity=10)
        j.append(kind=JournalRecordKind.EVENT, name="e1", session_id="s1")
        j.close()  # should not raise
        # Records are still readable after close
        assert len(j.read()) == 1

    def test_flush_is_noop(self):
        j = InMemoryRingBuffer(capacity=10)
        j.flush()  # should not raise

    def test_not_degraded_by_default(self):
        j = InMemoryRingBuffer(capacity=10)
        assert j.degraded is False

    def test_timing_auto_populated(self):
        j = InMemoryRingBuffer(capacity=10)
        j.append(kind=JournalRecordKind.EVENT, name="e1", session_id="s1")
        rec = j.read()[0]
        assert rec.timing.wall_ns > 0
        assert rec.timing.mono_ns > 0

    def test_turn_id_stored(self):
        j = InMemoryRingBuffer(capacity=10)
        j.append(
            kind=JournalRecordKind.EVENT,
            name="e1",
            session_id="s1",
            turn_id="t1",
        )
        rec = j.read()[0]
        assert rec.turn_id == "t1"

    def test_error_stored(self):
        from easycat.runtime.records import ErrorInfo

        j = InMemoryRingBuffer(capacity=10)
        err = ErrorInfo(type="ValueError", message="bad")
        j.append(
            kind=JournalRecordKind.EVENT,
            name="e1",
            session_id="s1",
            error=err,
        )
        rec = j.read()[0]
        assert rec.error is not None
        assert rec.error.type == "ValueError"

    def test_tags_stored(self):
        j = InMemoryRingBuffer(capacity=10)
        j.append(
            kind=JournalRecordKind.EVENT,
            name="e1",
            session_id="s1",
            tags=frozenset({"important"}),
        )
        rec = j.read()[0]
        assert "important" in rec.tags


class TestInMemoryRingBufferThreadSafety:
    def test_concurrent_appends(self):
        j = InMemoryRingBuffer(capacity=10_000)
        n_threads = 4
        n_per_thread = 250

        def writer(thread_id: int):
            for i in range(n_per_thread):
                j.append(
                    kind=JournalRecordKind.EVENT,
                    name=f"t{thread_id}_e{i}",
                    session_id="s1",
                )

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        records = j.read()
        assert len(records) == n_threads * n_per_thread
        seqs = [r.sequence for r in records]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)  # no duplicates


class TestDegradedMode:
    def test_degraded_on_internal_error(self, caplog):
        j = InMemoryRingBuffer(capacity=10)
        # Simulate a broken internal by making _do_append raise

        def broken(*args, **kwargs):
            raise RuntimeError("disk full")

        j._do_append = broken

        with caplog.at_level(logging.WARNING, logger="easycat"):
            seq = j.append(
                kind=JournalRecordKind.EVENT,
                name="e1",
                session_id="s1",
            )
        assert seq == -1
        assert j.degraded is True

        # The degraded transition should emit a WARNING on the easycat logger.
        assert "Journal entered degraded mode" in caplog.text
        assert any(rec.levelno == logging.WARNING for rec in caplog.records)

    def test_degraded_marker_does_not_advance_sequence(self):
        j = InMemoryRingBuffer(capacity=10)
        j.append(kind=JournalRecordKind.EVENT, name="e1", session_id="s1")
        seq_before = j.latest_sequence

        def broken(*args, **kwargs):
            raise RuntimeError("disk full")

        j._do_append = broken
        assert j.append(kind=JournalRecordKind.EVENT, name="e2", session_id="s1") == -1

        # The degraded marker occupies sequence -1 and the live counter does
        # not advance past a sequence no append() return value corresponds to.
        assert j.latest_sequence == seq_before
        degraded = [r for r in j.read(start=-1) if r.kind == JournalRecordKind.DEGRADED]
        assert len(degraded) == 1
        assert degraded[0].sequence == -1

    def test_degraded_signalled_via_property_not_record_stream(self):
        # The degraded marker at sequence=-1 is a deliberate out-of-band signal:
        # normal consumers detect degradation via the ``degraded`` property, NOT
        # by scanning read()/follow().  Assert the contract an actual consumer
        # relies on rather than the artificial read(start=-1) probe.
        j = InMemoryRingBuffer(capacity=10)
        j.append(kind=JournalRecordKind.EVENT, name="e1", session_id="s1")

        def broken(*args, **kwargs):
            raise RuntimeError("disk full")

        j._do_append = broken
        assert j.append(kind=JournalRecordKind.EVENT, name="e2", session_id="s1") == -1

        # The property is the in-band liveness signal.
        assert j.degraded is True
        assert JournalView(j).degraded is True

        # The marker is intentionally excluded from the normal read() path
        # (read filters sequence >= start, and the default start is 0).
        normal = j.read()
        assert all(r.kind != JournalRecordKind.DEGRADED for r in normal)

    def test_subsequent_appends_silently_dropped(self, caplog):
        j = InMemoryRingBuffer(capacity=10)
        j._degraded = True

        with caplog.at_level(logging.WARNING, logger="easycat"):
            seq = j.append(
                kind=JournalRecordKind.EVENT,
                name="e1",
                session_id="s1",
            )
        assert seq == -1
        # No warning is logged for subsequent drops once already degraded.
        assert caplog.records == []


class TestJournalView:
    def test_read_delegates(self):
        j = InMemoryRingBuffer(capacity=100)
        j.append(kind=JournalRecordKind.EVENT, name="e1", session_id="s1")
        view = JournalView(j)
        records = view.read()
        assert len(records) == 1

    def test_slice_delegates(self):
        j = InMemoryRingBuffer(capacity=100)
        j.append(kind=JournalRecordKind.EVENT, name="e1", session_id="s1")
        j.append(kind=JournalRecordKind.METRIC, name="m1", session_id="s1")
        view = JournalView(j)
        events = view.slice(kind=JournalRecordKind.EVENT)
        assert len(events) == 1

    def test_enabled(self):
        j = InMemoryRingBuffer(capacity=10)
        view = JournalView(j)
        assert view.enabled is True

    def test_degraded(self):
        j = InMemoryRingBuffer(capacity=10)
        view = JournalView(j)
        assert view.degraded is False
        j._degraded = True
        assert view.degraded is True

    async def test_follow(self):
        j = InMemoryRingBuffer(capacity=100)
        view = JournalView(j)

        received: list[int] = []

        async def follower():
            async for rec in view.follow(poll_interval=0.01):
                received.append(rec.sequence)
                if len(received) >= 3:
                    break

        # Append records in a separate task after a small delay
        async def appender():
            await asyncio.sleep(0.02)
            for i in range(3):
                j.append(
                    kind=JournalRecordKind.EVENT,
                    name=f"e{i}",
                    session_id="s1",
                )
                await asyncio.sleep(0.01)

        await asyncio.gather(
            asyncio.wait_for(follower(), timeout=2.0),
            appender(),
        )
        assert received == [1, 2, 3]

    async def test_follow_stop_event(self):
        j = InMemoryRingBuffer(capacity=10)
        view = JournalView(j)
        stop = asyncio.Event()

        async def follower() -> list[int]:
            seen: list[int] = []
            async for rec in view.follow(from_sequence=0, poll_interval=0.01, stop=stop):
                seen.append(rec.sequence)
            return seen

        task = asyncio.create_task(follower())
        await asyncio.sleep(0.03)
        stop.set()
        seen = await asyncio.wait_for(task, timeout=2.0)
        # The generator terminated cleanly once stop was set.
        assert seen == []

    async def test_follow_emits_gap_notice_on_eviction(self):
        # Capacity 2 so older records are evicted before follow() reads them.
        j = InMemoryRingBuffer(capacity=2)
        view = JournalView(j)
        # Append enough that the earliest sequences (1, 2, ...) are evicted.
        for i in range(6):
            j.append(kind=JournalRecordKind.EVENT, name=f"e{i}", session_id="s1")

        # Follow from sequence 1 — those records are long gone from the ring.
        gen = view.follow(from_sequence=1, poll_interval=0.01)
        first = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        await gen.aclose()

        assert first.kind == JournalRecordKind.CONTROL
        assert first.data["dropped_from"] == "follow_gap"
        assert first.data["gap"] >= 1
        assert first.sequence == 1

    async def test_follow_from_zero_does_not_emit_spurious_gap(self):
        # from_sequence=0 is the documented "replay full history then live-tail"
        # cursor.  Real sequences start at 1, so cursor=0 pointing below the
        # first sequence must NOT be reported as an eviction gap: the first
        # yielded record must be the real record at sequence 1.
        j = InMemoryRingBuffer(capacity=100)
        view = JournalView(j)
        j.append(kind=JournalRecordKind.EVENT, name="e1", session_id="s1")
        j.append(kind=JournalRecordKind.EVENT, name="e2", session_id="s1")

        gen = view.follow(from_sequence=0, poll_interval=0.01)
        first = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        await gen.aclose()

        # Not a synthetic follow_gap notice — a real record at sequence 1.
        assert first.sequence == 1
        assert first.name == "e1"
        assert "dropped_from" not in first.data


class TestCreateJournal:
    def test_returns_ring_buffer(self):
        j = create_journal("test-session")
        assert isinstance(j, InMemoryRingBuffer)

    def test_light_returns_ring_buffer(self):
        j = create_journal("test-session", debug="light")
        assert isinstance(j, InMemoryRingBuffer)

    def test_custom_capacity(self):
        j = create_journal("test-session", capacity=50)
        assert j._capacity == 50

    def test_full_returns_sqlite(self, tmp_path):
        from easycat.runtime.journal import SqliteJournal

        j = create_journal("test-session", debug="full", data_dir=str(tmp_path))
        assert isinstance(j, SqliteJournal)
        j.close()
