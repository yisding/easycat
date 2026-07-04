"""Tests for the ArtifactStore backends."""

from __future__ import annotations

import hashlib

from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.artifacts import (
    FilesystemArtifactStore,
    InMemoryArtifactStore,
)
from easycat.runtime.records import JournalRecordKind


class TestInMemoryArtifactStore:
    def test_put_and_get(self):
        store = InMemoryArtifactStore()
        ref = store.put(b"hello world")
        assert ref == hashlib.sha256(b"hello world").hexdigest()
        assert store.get(ref) == b"hello world"

    def test_dedup(self):
        store = InMemoryArtifactStore()
        ref1 = store.put(b"dup")
        ref2 = store.put(b"dup")
        assert ref1 == ref2

    def test_has(self):
        store = InMemoryArtifactStore()
        ref = store.put(b"data")
        assert store.has(ref)
        assert not store.has("nonexistent")

    def test_delete(self):
        store = InMemoryArtifactStore()
        ref = store.put(b"data")
        store.delete(ref)
        assert not store.has(ref)
        assert store.get(ref) is None

    def test_refuses_new_writes_past_cap(self):
        """Once the byte cap is reached, new artifacts are refused (return "")
        while prior artifacts still resolve — referenced blobs are never evicted."""
        store = InMemoryArtifactStore(max_bytes=100)
        ref1 = store.put(b"a" * 60)
        assert ref1
        assert store.has(ref1)

        # Second 60-byte write would push the total to 120 > 100: refused.
        ref2 = store.put(b"b" * 60)
        assert ref2 == ""
        assert not store.has(hashlib.sha256(b"b" * 60).hexdigest())

        # The earlier artifact is untouched — the in-memory store never evicts
        # blobs that a still-buffered journal row may already reference.
        assert store.get(ref1) == b"a" * 60

    def test_cap_refusal_keeps_referenced_blobs_resolvable(self, caplog):
        """Fill past the byte cap while a record still references an early blob;
        the referenced blob stays resolvable and exactly one warning is logged."""
        import logging

        store = InMemoryArtifactStore(max_bytes=100)
        buf = InMemoryRingBuffer(capacity=8, artifact_store=store)

        early = store.put(b"a" * 60)
        assert early
        buf.append(
            kind=JournalRecordKind.EVENT,
            name="test",
            session_id="s1",
            input_ref=early,
        )

        with caplog.at_level(logging.WARNING, logger="easycat.runtime.artifacts"):
            refused = store.put(b"b" * 60)  # would exceed the 100-byte cap
            again = store.put(b"c" * 60)  # refused again, no second warning

        # Over-cap writes are refused, not silently swallowing the early blob.
        assert refused == ""
        assert again == ""
        # The still-referenced early blob remains resolvable — no dangling ref.
        assert store.has(early)
        assert store.get(early) == b"a" * 60

        cap_warnings = [r for r in caplog.records if "reached max_bytes" in r.getMessage()]
        assert len(cap_warnings) == 1

    def test_close_clears(self):
        store = InMemoryArtifactStore()
        ref = store.put(b"data")
        store.close()
        assert not store.has(ref)

    def test_get_missing_returns_none(self):
        store = InMemoryArtifactStore()
        assert store.get("missing") is None

    def test_large_payload_stored_by_ref_keeps_record_small(self):
        """A 1MB artifact lives in the store; the record only carries a ref."""
        import json

        store = InMemoryArtifactStore()
        journal = InMemoryRingBuffer(capacity=100)

        payload = b"\x00" * 1_000_000
        ref = store.put(payload)
        assert ref == hashlib.sha256(payload).hexdigest()

        seq = journal.append(
            kind=JournalRecordKind.EVENT,
            name="audio_capture",
            session_id="s",
            input_ref=ref,
        )
        rec = journal.read(start=seq, limit=1)[0]
        assert rec.input_ref == ref
        assert len(json.dumps(rec.data)) < 4096


class TestFilesystemArtifactStore:
    def test_put_and_get(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        ref = store.put(b"hello fs")
        expected = hashlib.sha256(b"hello fs").hexdigest()
        assert ref == expected
        assert store.get(ref) == b"hello fs"

    def test_file_created(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        ref = store.put(b"data")
        path = tmp_path / "artifacts" / "sess" / f"{ref}.bin"
        assert path.exists()

    def test_dedup_no_rewrite(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        ref1 = store.put(b"same")
        ref2 = store.put(b"same")
        assert ref1 == ref2

    def test_has_and_delete(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        ref = store.put(b"data")
        assert store.has(ref)
        store.delete(ref)
        assert not store.has(ref)

    def test_get_missing_returns_none(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        assert store.get("nonexistent") is None

    def test_get_head_tail_reads_bounded_window(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        payload = b"a" * 10 + b"middle" * 20 + b"z" * 10
        ref = store.put(payload)
        assert store.get_head_tail(ref, byte_cap=10) == b"a" * 10 + b"z" * 10

    def test_get_head_tail_returns_small_payloads_unchanged(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        ref = store.put(b"small")
        assert store.get_head_tail(ref, byte_cap=10) == b"small"

    def test_permissions(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        ref = store.put(b"secret data")
        path = tmp_path / "artifacts" / "sess" / f"{ref}.bin"
        assert path.stat().st_mode & 0o777 == 0o600

    def test_max_bytes_refuses_new_writes_past_cap(self, tmp_path):
        """Once the byte cap is reached, new artifacts are refused (return "")
        while prior artifacts still resolve — durable bytes are never evicted."""
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=100)
        ref1 = store.put(b"a" * 60)
        assert ref1
        assert store.get(ref1) == b"a" * 60

        # Second 60-byte write would push the total to 120 > 100: refused.
        ref2 = store.put(b"b" * 60)
        assert ref2 == ""
        assert not store.has(hashlib.sha256(b"b" * 60).hexdigest())

        # The earlier artifact is untouched — the filesystem store never evicts
        # durable bytes that a journal row may already reference.
        assert store.get(ref1) == b"a" * 60

    def test_max_bytes_logs_a_single_warning(self, tmp_path, caplog):
        import logging

        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        store.put(b"under-cap")  # 9 bytes, fits
        with caplog.at_level(logging.WARNING, logger="easycat.runtime.artifacts"):
            store.put(b"x" * 50)  # refused
            store.put(b"y" * 50)  # refused again, but no second warning
        cap_warnings = [r for r in caplog.records if "reached max_bytes" in r.getMessage()]
        assert len(cap_warnings) == 1

    def test_dedup_does_not_double_count_against_cap(self, tmp_path):
        """Re-putting identical content already on disk does not consume cap
        budget, so a duplicate write never trips the cap on its own."""
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=70)
        payload = b"c" * 60
        ref1 = store.put(payload)
        assert ref1
        # Same content: dedup short-circuits before the cap check, so it still
        # succeeds even though 60 + 60 would exceed 70.
        ref2 = store.put(payload)
        assert ref2 == ref1
        # A genuinely new 60-byte payload would exceed the cap and be refused.
        assert store.put(b"d" * 60) == ""


class TestRingBufferArtifactEviction:
    """Verify that InMemoryRingBuffer evicts orphaned artifacts on overflow."""

    def _append(
        self,
        buf: InMemoryRingBuffer,
        *,
        input_ref: str | None = None,
        output_ref: str | None = None,
    ) -> int:
        return buf.append(
            kind=JournalRecordKind.EVENT,
            name="test",
            session_id="s1",
            input_ref=input_ref,
            output_ref=output_ref,
        )

    def test_orphaned_artifact_evicted_on_overflow(self):
        """Artifacts referenced only by evicted records are deleted.

        Note: the first overflow also appends a ``BufferOverflow`` marker,
        which itself evicts the next oldest record.  We use capacity=5 to
        give room for the marker without surprising extra evictions.
        """
        store = InMemoryArtifactStore()
        buf = InMemoryRingBuffer(capacity=5, artifact_store=store)

        ref_a = store.put(b"artifact-a")
        ref_b = store.put(b"artifact-b")

        # Fill the buffer: slots 1..5.
        # Records 1-3 reference ref_a, record 4 references ref_b, record 5 is plain.
        self._append(buf, input_ref=ref_a)  # record 1
        self._append(buf, input_ref=ref_a)  # record 2
        self._append(buf, input_ref=ref_a)  # record 3
        self._append(buf, output_ref=ref_b)  # record 4
        self._append(buf)  # record 5

        assert store.has(ref_a)
        assert store.has(ref_b)

        # Overflow: record 1 (ref_a) evicted + BufferOverflow marker evicts record 2.
        # ref_a count: 3 -> 2 -> 1. Still alive via record 3.
        self._append(buf)
        assert store.has(ref_a), "ref_a should survive — record 3 still references it"
        assert store.has(ref_b), "ref_b should survive — record 4 still references it"

        # Next overflow: record 3 (last ref_a holder) evicted.
        self._append(buf)
        assert not store.has(ref_a), "ref_a should be deleted — no remaining references"
        assert store.has(ref_b), "ref_b should survive — record 4 still in buffer"

    def test_retained_artifact_survives(self):
        """Artifacts still referenced by retained records are not deleted.

        Uses capacity=6 so the single BufferOverflow marker (appended on
        the first overflow) does not interfere with the eviction counting.
        """
        store = InMemoryArtifactStore()
        buf = InMemoryRingBuffer(capacity=6, artifact_store=store)

        ref = store.put(b"shared-artifact")

        # Fill: 5 records reference the artifact, 1 plain padding slot.
        self._append(buf, input_ref=ref)  # record 1
        self._append(buf, input_ref=ref)  # record 2
        self._append(buf, input_ref=ref)  # record 3
        self._append(buf, input_ref=ref)  # record 4
        self._append(buf, input_ref=ref)  # record 5
        self._append(buf)  # record 6 (padding)

        # First overflow evicts record 1 + marker evicts record 2.  3 refs remain.
        self._append(buf)
        assert store.has(ref), "3 remaining records still reference the artifact"

        # Evict record 3.  2 refs remain.
        self._append(buf)
        assert store.has(ref), "2 remaining records still reference the artifact"

        # Evict record 4.  1 ref remains.
        self._append(buf)
        assert store.has(ref), "1 remaining record still references the artifact"

        # Evict record 5 — last reference gone.
        self._append(buf)
        assert not store.has(ref), "artifact should be deleted — zero references"

    def test_no_artifact_store_does_not_crash(self):
        """Buffer without an artifact store ignores ref tracking gracefully."""
        buf = InMemoryRingBuffer(capacity=2)
        self._append(buf, input_ref="some-ref")
        self._append(buf, output_ref="other-ref")
        # Overflow — should not raise.
        self._append(buf)

    def test_input_and_output_refs_both_tracked(self):
        """Both input_ref and output_ref are tracked and evicted correctly."""
        store = InMemoryArtifactStore()
        buf = InMemoryRingBuffer(capacity=2, artifact_store=store)

        ref_in = store.put(b"input-data")
        ref_out = store.put(b"output-data")

        self._append(buf, input_ref=ref_in, output_ref=ref_out)
        self._append(buf)

        assert store.has(ref_in)
        assert store.has(ref_out)

        # Evict the record carrying both refs.
        self._append(buf)
        assert not store.has(ref_in)
        assert not store.has(ref_out)
