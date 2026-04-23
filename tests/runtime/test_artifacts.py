"""Tests for the ArtifactStore backends."""

from __future__ import annotations

import hashlib

from easycat.runtime.artifacts import (
    FilesystemArtifactStore,
    InMemoryArtifactStore,
)
from easycat.runtime.journal import InMemoryRingBuffer
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

    def test_eviction(self):
        store = InMemoryArtifactStore(max_bytes=100)
        ref1 = store.put(b"a" * 60)
        ref2 = store.put(b"b" * 60)
        # ref1 should have been evicted to make room for ref2.
        assert not store.has(ref1)
        assert store.has(ref2)

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

    def test_permissions(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        ref = store.put(b"secret data")
        path = tmp_path / "artifacts" / "sess" / f"{ref}.bin"
        assert path.stat().st_mode & 0o777 == 0o600


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
