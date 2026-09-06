"""Tests for ExecutionJournal, InMemoryRingBuffer, and JournalView."""

from __future__ import annotations

import asyncio
import collections
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Protocol

import pytest

from easycat.runtime import ExecutionJournal, InMemoryRingBuffer, JournalView, create_journal
from easycat.runtime.records import ErrorInfo, JournalRecord, JournalRecordKind
from easycat.validation.redaction import REDACTED_PHONE, REDACTED_SECRET


class _JournalQuery(Protocol):
    def read(self, start: int = 0, limit: int | None = None) -> list[JournalRecord]: ...

    def slice(
        self,
        *,
        turn_id: str | None = None,
        name: str | None = None,
        tags: frozenset[str] | None = None,
    ) -> list[JournalRecord]: ...


async def _yield_to_scheduled_tasks() -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    loop.call_soon(ready.set_result, None)
    await ready


class TestInMemoryRingBuffer:
    @pytest.mark.parametrize("capacity", [0, -1, True, 1.5])
    def test_rejects_invalid_capacity(self, capacity):
        with pytest.raises(ValueError, match="capacity must be a positive integer"):
            InMemoryRingBuffer(capacity=capacity)  # type: ignore[arg-type]

    def test_rejects_unknown_redaction_policy(self):
        with pytest.raises(ValueError, match="Unknown redaction policy"):
            InMemoryRingBuffer(redaction="everything")  # type: ignore[arg-type]

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

    def test_append_applies_write_filter(self):
        j = InMemoryRingBuffer(capacity=100)
        j.append(
            kind=JournalRecordKind.EVENT,
            name="sensitive",
            session_id="s1",
            data={
                "text": "phone +1 415 555 1212",
                "api_key": "short",
            },
            error=ErrorInfo(
                type="RuntimeError",
                message="Authorization: Bearer sk-testsecret123456",
            ),
        )

        record = j.read()[0]
        assert record.data == {
            "api_key": REDACTED_SECRET,
            "text": "phone +1 415 555 1212",
        }
        assert record.error is not None
        assert record.error.message == f"Authorization: {REDACTED_SECRET}"

    def test_append_can_apply_pii_write_filter(self):
        j = InMemoryRingBuffer(capacity=100, redaction="pii")
        j.append(
            kind=JournalRecordKind.EVENT,
            name="sensitive",
            session_id="s1",
            data={"text": "phone +1 415 555 1212"},
        )

        assert j.read()[0].data["text"] == f"phone {REDACTED_PHONE}"

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

    def test_read_rejects_negative_limit(self):
        j = InMemoryRingBuffer(capacity=100)
        for i in range(3):
            j.append(kind=JournalRecordKind.EVENT, name=f"e{i}", session_id="s1")

        with pytest.raises(ValueError, match="limit"):
            j.read(limit=-1)
        with pytest.raises(ValueError, match="limit"):
            j.snapshot().read(limit=-1)

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

    def test_slice_by_turn_id(self):
        j = InMemoryRingBuffer(capacity=100)
        j.append(kind=JournalRecordKind.EVENT, name="e1", session_id="s1", turn_id="t1")
        j.append(kind=JournalRecordKind.EVENT, name="e2", session_id="s1", turn_id="t2")
        assert [r.name for r in j.slice(turn_id="t1")] == ["e1"]

    def test_slice_by_name(self):
        j = InMemoryRingBuffer(capacity=100)
        j.append(kind=JournalRecordKind.EVENT, name="stt_final", session_id="s1")
        j.append(kind=JournalRecordKind.EVENT, name="tts_frame", session_id="s1")
        assert [r.name for r in j.slice(name="stt_final")] == ["stt_final"]

    def test_slice_by_tags_subset(self):
        j = InMemoryRingBuffer(capacity=100)
        j.append(
            kind=JournalRecordKind.EVENT,
            name="e1",
            session_id="s1",
            tags=frozenset({"a", "b"}),
        )
        j.append(
            kind=JournalRecordKind.EVENT,
            name="e2",
            session_id="s1",
            tags=frozenset({"a"}),
        )
        # ``a`` is a subset of both; ``a+b`` only matches the first record.
        assert {r.name for r in j.slice(tags=frozenset({"a"}))} == {"e1", "e2"}
        assert [r.name for r in j.slice(tags=frozenset({"a", "b"}))] == ["e1"]

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
        assert len(overflow_records) == 1
        assert overflow_records[0].name == "buffer_overflow"
        assert overflow_records[0].data["dropped_records"] == j.dropped_records
        assert j.dropped_records > 0
        assert JournalView(j).dropped_records == j.dropped_records

    def test_capacity_one_keeps_the_latest_real_record_after_overflow(self):
        j = InMemoryRingBuffer(capacity=1)
        for i in range(5):
            j.append(kind=JournalRecordKind.EVENT, name=f"e{i}", session_id="s1")

        # A one-record buffer has no room for both an event and a separate
        # loss marker.  It must retain the newest useful event rather than
        # evicting it to preserve the marker.
        assert [record.name for record in j.read()] == ["e4"]
        assert j.dropped_records == 4

    def test_refs_do_not_accumulate_without_an_artifact_store(self):
        j = InMemoryRingBuffer(capacity=2)
        for i in range(20):
            j.append(
                kind=JournalRecordKind.EVENT,
                name=f"e{i}",
                session_id="s1",
                input_ref=f"ref-{i}",
            )

        assert j._ref_counts == {}

    def test_long_overflow_keeps_loss_visible(self):
        j = InMemoryRingBuffer(capacity=1_000)
        for i in range(3_000):
            j.append(kind=JournalRecordKind.EVENT, name=f"e{i}", session_id="s1")

        records = j.read()
        overflow_records = [record for record in records if record.name == "buffer_overflow"]

        assert len(records) == 1_000
        assert records[0].sequence > 1
        assert len(overflow_records) == 1
        assert j.dropped_records >= 2_000
        assert overflow_records[0].data["dropped_records"] == j.dropped_records

        snapshot = j.snapshot()
        assert snapshot.dropped_records == j.dropped_records

    def test_overflow_append_does_not_scan_the_ring(self):
        class _NoIterationDeque(collections.deque):
            def __iter__(self):
                raise AssertionError("the append hot path must not scan the ring")

        j = InMemoryRingBuffer(capacity=10)
        for i in range(11):
            j.append(kind=JournalRecordKind.EVENT, name=f"e{i}", session_id="s1")

        j._buf = _NoIterationDeque(j._buf, maxlen=10)
        j.append(kind=JournalRecordKind.EVENT, name="after-overflow", session_id="s1")

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
    def test_concurrent_degradation_attempts_emit_one_marker(self):
        """Only the first racing append may publish the degraded transition."""
        j = InMemoryRingBuffer(capacity=10)
        barrier = threading.Barrier(2)
        results: list[int] = []

        def broken(*_args, **_kwargs):
            barrier.wait(timeout=1.0)
            raise RuntimeError("disk full")

        j._do_append = broken

        def append() -> None:
            results.append(j.append(kind=JournalRecordKind.EVENT, name="e", session_id="s1"))

        first = threading.Thread(target=append)
        second = threading.Thread(target=append)
        first.start()
        second.start()
        first.join(timeout=2.0)
        second.join(timeout=2.0)

        assert not first.is_alive()
        assert not second.is_alive()
        assert results == [-1, -1]
        degraded = [
            record for record in j.read(start=-1) if record.kind == JournalRecordKind.DEGRADED
        ]
        assert len(degraded) == 1

    def test_append_that_entered_before_degradation_cannot_write_afterwards(self):
        """The degraded-state decision and normal append must be serialized."""
        j = InMemoryRingBuffer(capacity=10)
        original_do_append = j._do_append
        entered = threading.Event()
        release = threading.Event()
        results: list[int] = []

        def delayed(*args, **kwargs):
            entered.set()
            assert release.wait(timeout=1.0)
            return original_do_append(*args, **kwargs)

        j._do_append = delayed

        late_append = threading.Thread(
            target=lambda: results.append(
                j.append(kind=JournalRecordKind.EVENT, name="late", session_id="s1")
            )
        )
        late_append.start()
        try:
            assert entered.wait(timeout=1.0)
            j._enter_degraded("s1", RuntimeError("disk full"))
        finally:
            release.set()
            late_append.join(timeout=2.0)

        assert not late_append.is_alive()
        assert results == [-1]
        assert [record.name for record in j.read(start=-1)] == ["journal_degraded"]

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

    def test_snapshot_latest_sequence_ignores_degraded_marker(self):
        # A degraded in-memory journal preserved as a FrozenJournalSnapshot
        # (via Session.stop() postmortem) must report the same latest_sequence
        # as the live buffer, not the out-of-band -1 degraded marker.
        j = InMemoryRingBuffer(capacity=10)
        j.append(kind=JournalRecordKind.EVENT, name="e1", session_id="s1")
        j.append(kind=JournalRecordKind.EVENT, name="e2", session_id="s1")
        seq_before = j.latest_sequence

        def broken(*args, **kwargs):
            raise RuntimeError("disk full")

        j._do_append = broken
        assert j.append(kind=JournalRecordKind.EVENT, name="e3", session_id="s1") == -1
        assert j.degraded is True

        snapshot = j.snapshot()
        # Last buffered record is the degraded marker at sequence -1, but the
        # snapshot must still report the live counter value.
        assert snapshot.latest_sequence == seq_before

    def test_capacity_one_snapshot_preserves_live_sequence_after_degradation(self):
        j = InMemoryRingBuffer(capacity=1)
        assert j.append(kind=JournalRecordKind.EVENT, name="e1", session_id="s1") == 1

        def broken(*args, **kwargs):
            raise RuntimeError("disk full")

        j._do_append = broken
        assert j.append(kind=JournalRecordKind.EVENT, name="e2", session_id="s1") == -1
        assert [record.sequence for record in j.slice()] == [-1]

        snapshot = j.snapshot()
        assert snapshot.latest_sequence == j.latest_sequence == 1

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
            async for rec in view.follow(poll_interval=0.001):
                received.append(rec.sequence)
                if len(received) >= 3:
                    break

        follower_task = asyncio.create_task(follower())
        await _yield_to_scheduled_tasks()

        for i in range(3):
            j.append(
                kind=JournalRecordKind.EVENT,
                name=f"e{i}",
                session_id="s1",
            )

        await asyncio.wait_for(follower_task, timeout=2.0)
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


class TestReadonlySqliteFollow:
    """``JournalView.follow`` over a ``ReadonlySqliteJournal`` (the live-tail
    backend used by ``easycat journal follow`` / ``easycat tail``)."""

    async def test_follow_yields_appended_records(self, tmp_path):
        from easycat.runtime import SqliteJournal
        from easycat.runtime.journal_views import ReadonlySqliteJournal

        writer = SqliteJournal("tail-sess", data_dir=str(tmp_path))
        try:
            writer.append(kind=JournalRecordKind.EVENT, name="first", session_id="tail-sess")

            # The readonly view re-opens the file each query, so it observes
            # records the writer commits after the view was constructed.
            db_path = tmp_path / "journals" / "tail-sess.sqlite"
            view = JournalView(ReadonlySqliteJournal(db_path))

            received: list[str] = []

            async def follower() -> None:
                async for rec in view.follow(from_sequence=0, poll_interval=0.01):
                    received.append(rec.name)
                    if len(received) >= 2:
                        break

            task = asyncio.create_task(follower())
            await _yield_to_scheduled_tasks()
            writer.append(kind=JournalRecordKind.EVENT, name="second", session_id="tail-sess")
            await asyncio.wait_for(task, timeout=3.0)

            assert received == ["first", "second"]
        finally:
            writer.close()

    async def test_followed_records_format_with_follow_line(self, tmp_path):
        # Acceptance: a followed record renders through the CLI formatter and a
        # tts_frame yields the per-turn milestone landmark + audio bar.
        from easycat.cli.debug.follow import _format_follow_line, _record_to_follow_dict
        from easycat.runtime import SqliteJournal
        from easycat.runtime.journal_views import ReadonlySqliteJournal

        writer = SqliteJournal("fmt-sess", data_dir=str(tmp_path))
        try:
            writer.append(
                kind=JournalRecordKind.EVENT,
                name="tts_frame",
                session_id="fmt-sess",
                turn_id="t1",
                data={"audio_bytes": 4096},
            )
            db_path = tmp_path / "journals" / "fmt-sess.sqlite"
            view = JournalView(ReadonlySqliteJournal(db_path))

            gen = view.follow(from_sequence=0, poll_interval=0.01)
            rec = await asyncio.wait_for(gen.__anext__(), timeout=3.0)
            await gen.aclose()

            line = _format_follow_line(_record_to_follow_dict(rec))
            assert "name=tts_frame" in line
            assert "milestone=tts_first_byte" in line
            assert "audio=4096B" in line
        finally:
            writer.close()


class TestReadonlyStageSlices:
    """``slice_by_stage`` on the postmortem read-only journal implementations."""

    @staticmethod
    def _seed(tmp_path):
        from easycat.runtime import SqliteJournal

        writer = SqliteJournal("stage-sess", data_dir=str(tmp_path))
        try:
            writer.append(
                kind=JournalRecordKind.EVENT,
                name="direct",
                session_id="stage-sess",
                data={"stage": "stt"},
            )
            writer.append(
                kind=JournalRecordKind.CONTROL,
                name="observed",
                session_id="stage-sess",
                data={"stage": "agent", "observed_stage": "stt"},
            )
            writer.append(
                kind=JournalRecordKind.EVENT,
                name="other",
                session_id="stage-sess",
                data={"stage": "tts"},
            )
            writer.append(kind=JournalRecordKind.EVENT, name="bare", session_id="stage-sess")
        finally:
            writer.close()
        return tmp_path / "journals" / "stage-sess.sqlite"

    def test_readonly_sqlite_uses_indexed_stage_columns(self, tmp_path):
        from easycat.runtime.journal_views import ReadonlySqliteJournal

        db_path = self._seed(tmp_path)
        view = ReadonlySqliteJournal(db_path)

        assert [r.name for r in view.slice_by_stage("stt")] == ["direct", "observed"]
        assert [r.name for r in view.slice_by_stage("agent")] == ["observed"]
        assert [r.name for r in view.slice_by_stage("tts")] == ["other"]
        assert view.slice_by_stage("missing") == []

    def test_readonly_sqlite_falls_back_to_scan_for_legacy_schema(self, tmp_path):
        from easycat.runtime.journal_views import ReadonlySqliteJournal

        db_path = self._seed(tmp_path)
        # Strip the derived stage columns to mimic a journal written before
        # they existed; the read-only view cannot ALTER the file to add them.
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("DROP INDEX IF EXISTS idx_journal_stage")
            conn.execute("DROP INDEX IF EXISTS idx_journal_observed_stage")
            conn.execute("ALTER TABLE journal DROP COLUMN stage")
            conn.execute("ALTER TABLE journal DROP COLUMN observed_stage")
            conn.commit()
        finally:
            conn.close()
        view = ReadonlySqliteJournal(db_path)

        assert [r.name for r in view.slice_by_stage("stt")] == ["direct", "observed"]
        assert [r.name for r in view.slice_by_stage("agent")] == ["observed"]
        assert view.slice_by_stage("missing") == []

    def test_readonly_sqlite_propagates_unrelated_operational_errors(self, tmp_path, monkeypatch):
        from easycat.runtime.journal_views import ReadonlySqliteJournal

        db_path = self._seed(tmp_path)
        view = ReadonlySqliteJournal(db_path)

        def locked(sql, params):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(view, "_query", locked)

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            view.slice_by_stage("stt")

    def test_litestream_wrapper_delegates_slice_by_stage(self, tmp_path: Path) -> None:
        # Without the delegator ``JournalView.filter_by_stage`` sees no
        # ``slice_by_stage`` on the wrapper and deserializes every record
        # instead of using the inner journal's stage indexes (gh 1026).
        from easycat.runtime import LitestreamSqliteJournal
        from easycat.runtime.journal import JournalView

        j = LitestreamSqliteJournal("stage-sess", data_dir=tmp_path)
        try:
            j.append(
                kind=JournalRecordKind.EVENT,
                name="direct",
                session_id="stage-sess",
                data={"stage": "stt"},
            )
            j.append(
                kind=JournalRecordKind.CONTROL,
                name="observed",
                session_id="stage-sess",
                data={"stage": "agent", "observed_stage": "stt"},
            )
            j.append(
                kind=JournalRecordKind.EVENT,
                name="other",
                session_id="stage-sess",
                data={"stage": "tts"},
            )

            assert [r.name for r in j.slice_by_stage("stt")] == ["direct", "observed"]
            assert [r.name for r in j.slice_by_stage("tts")] == ["other"]
            assert j.slice_by_stage("missing") == []

            # ``JournalView`` must reach the indexed path, not a record scan.
            # Both layers are watched: tracking only the wrapper's ``read``
            # would still pass for an implementation that scanned through
            # ``j._inner.read``.
            reads: list[str] = []
            indexed: list[str] = []
            inner_slice_by_stage = j._inner.slice_by_stage

            def _tracked_read(start: int = 0, limit: int | None = None) -> list[JournalRecord]:
                reads.append("read")
                raise AssertionError("filter_by_stage must not fall back to a record scan")

            def _tracked_slice_by_stage(stage_name: str) -> list[JournalRecord]:
                indexed.append(stage_name)
                return inner_slice_by_stage(stage_name)

            j.read = _tracked_read  # type: ignore[method-assign]
            j._inner.read = _tracked_read  # type: ignore[method-assign]
            j._inner.slice_by_stage = _tracked_slice_by_stage  # type: ignore[method-assign]

            assert [r.name for r in JournalView(j).filter_by_stage("stt")] == [
                "direct",
                "observed",
            ]
            assert reads == []
            assert indexed == ["stt"]
        finally:
            j.close()

    def test_frozen_snapshot_matches_stage_and_observed_stage(self):
        from easycat.runtime.journal_views import FrozenJournalSnapshot

        def _rec(seq: int, name: str, data: object) -> JournalRecord:
            return JournalRecord(
                sequence=seq,
                session_id="s",
                name=name,
                data=data,  # type: ignore[arg-type]
            )

        snapshot = FrozenJournalSnapshot(
            [
                _rec(1, "direct", {"stage": "stt"}),
                _rec(2, "observed", {"stage": "agent", "observed_stage": "stt"}),
                _rec(3, "other", {"stage": "tts"}),
                _rec(4, "not-a-dict", ["stt"]),
            ]
        )

        assert [r.name for r in snapshot.slice_by_stage("stt")] == ["direct", "observed"]
        assert [r.name for r in snapshot.slice_by_stage("agent")] == ["observed"]
        assert snapshot.slice_by_stage("missing") == []
        # Results are copies; mutating them cannot alter the frozen snapshot.
        snapshot.slice_by_stage("stt")[0].data["stage"] = "tts"
        assert [r.name for r in snapshot.slice_by_stage("stt")] == ["direct", "observed"]


class TestCreateJournal:
    @pytest.mark.parametrize(
        "session_id",
        [".", "..", "../escape", r"..\escape", "/absolute", "nested/session"],
    )
    def test_rejects_session_ids_that_can_escape_persistent_root(
        self,
        tmp_path,
        session_id: str,
    ) -> None:
        with pytest.raises(ValueError, match="session_id must"):
            create_journal(session_id, debug="full", data_dir=tmp_path)

        assert not (tmp_path.parent / "escape.sqlite").exists()

    def test_returns_ring_buffer(self):
        j = create_journal("test-session")
        assert isinstance(j, InMemoryRingBuffer)

    def test_in_memory_journal_does_not_apply_filesystem_id_rules(self):
        j = create_journal("../not-persisted")
        assert isinstance(j, InMemoryRingBuffer)

    def test_light_returns_ring_buffer(self):
        j = create_journal("test-session", debug="light")
        assert isinstance(j, InMemoryRingBuffer)

    def test_custom_capacity(self):
        j = create_journal("test-session", capacity=50)
        assert j._capacity == 50

    def test_full_returns_sqlite(self, tmp_path):
        from easycat.runtime import SqliteJournal

        j = create_journal("test-session", debug="full", data_dir=str(tmp_path))
        assert isinstance(j, SqliteJournal)
        j.close()


class TestSliceFiltersAcrossBackends:
    """``slice(turn_id=/name=/tags=)`` must WHERE-match on every backend,
    including the non-inheriting ``LitestreamSqliteJournal`` wrapper (which
    must forward the new kwargs to its inner SQLite journal without an
    arg-mismatch)."""

    def _seed(self, j: ExecutionJournal) -> None:
        j.append(
            kind=JournalRecordKind.EVENT,
            name="stt_final",
            session_id="sess",
            turn_id="t1",
            tags=frozenset({"slow", "stt"}),
        )
        j.append(
            kind=JournalRecordKind.EVENT,
            name="tts_frame",
            session_id="sess",
            turn_id="t2",
            tags=frozenset({"stt"}),
        )

    @staticmethod
    def _assert_in_memory_queries(j: _JournalQuery) -> None:
        assert [r.name for r in j.read(start=2, limit=1)] == ["tts_frame"]
        assert [r.name for r in j.slice(turn_id="t1")] == ["stt_final"]
        assert [r.name for r in j.slice(name="tts_frame")] == ["tts_frame"]
        assert {r.name for r in j.slice(tags=frozenset({"stt"}))} == {
            "stt_final",
            "tts_frame",
        }
        assert [r.name for r in j.slice(tags=frozenset({"slow"}))] == ["stt_final"]
        with pytest.raises(ValueError, match="limit"):
            j.read(limit=-1)

    def test_live_and_frozen_in_memory_queries_match(self) -> None:
        live = InMemoryRingBuffer(capacity=10)
        self._seed(live)
        frozen = live.snapshot()

        self._assert_in_memory_queries(live)
        self._assert_in_memory_queries(frozen)
        assert live.read() == frozen.read()

    def test_sqlite_slice_filters(self, tmp_path):
        from easycat.runtime import SqliteJournal

        j = SqliteJournal("sess", data_dir=tmp_path)
        try:
            self._seed(j)
            assert [r.name for r in j.slice(turn_id="t1")] == ["stt_final"]
            assert [r.name for r in j.slice(name="tts_frame")] == ["tts_frame"]
            # ``stt`` tags both rows; ``slow`` is unique to the first.
            assert {r.name for r in j.slice(tags=frozenset({"stt"}))} == {
                "stt_final",
                "tts_frame",
            }
            assert [r.name for r in j.slice(tags=frozenset({"slow"}))] == ["stt_final"]
        finally:
            j.close()

    def test_litestream_wrapper_slice_forwards_kwargs(self, tmp_path):
        # No replica URL configured → degrades to plain SQLite, but the
        # wrapper's slice() signature + forwarding must still accept and pass
        # the new filters through without an arg mismatch.
        from easycat.runtime import LitestreamSqliteJournal

        j = LitestreamSqliteJournal("sess", data_dir=tmp_path)
        try:
            self._seed(j)
            assert [r.name for r in j.slice(turn_id="t2")] == ["tts_frame"]
            assert [r.name for r in j.slice(name="stt_final")] == ["stt_final"]
            assert [r.name for r in j.slice(tags=frozenset({"slow"}))] == ["stt_final"]
        finally:
            j.close()
