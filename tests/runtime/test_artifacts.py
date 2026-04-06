"""Tests for the ArtifactStore backends."""

from __future__ import annotations

import hashlib

from easycat.runtime.artifacts import (
    FilesystemArtifactStore,
    InMemoryArtifactStore,
)


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
