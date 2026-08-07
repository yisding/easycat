"""Tests for the ArtifactStore backends."""

from __future__ import annotations

import errno
import gc
import hashlib
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from easycat.runtime import InMemoryRingBuffer
from easycat.runtime import artifacts as artifacts_module
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

    def test_conditional_cleanup_token_is_invalidated_by_later_put(self):
        store = InMemoryArtifactStore()
        receipt = store.put_with_cleanup_token(b"shared")
        assert receipt.created is True
        assert receipt.cleanup_token is not None

        duplicate = store.put_with_cleanup_token(b"shared")
        assert duplicate.ref == receipt.ref
        assert duplicate.created is False
        assert duplicate.cleanup_token is None
        assert store.delete_if_cleanup_token(receipt.ref, receipt.cleanup_token) is False
        assert store.has(receipt.ref)

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
    @pytest.mark.parametrize(
        "session_id",
        [".", "..", "../escape", r"..\escape", "/absolute", "nested/session"],
    )
    def test_rejects_session_ids_that_can_escape_artifact_root(
        self,
        tmp_path,
        session_id: str,
    ) -> None:
        with pytest.raises(ValueError, match="session_id must"):
            FilesystemArtifactStore(session_id, data_dir=tmp_path)

        assert not (tmp_path.parent / "escape").exists()

    def test_put_and_get(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        ref = store.put(b"hello fs")
        expected = hashlib.sha256(b"hello fs").hexdigest()
        assert ref == expected
        assert store.get(ref) == b"hello fs"

    def test_external_claim_wait_does_not_hold_instance_lock(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        waiting = threading.Event()
        release = threading.Event()
        unrelated_claimed = threading.Event()
        claim_counter_lock = threading.Lock()
        claim_count = 0
        errors: list[BaseException] = []

        @contextmanager
        def controlled_claim(
            target_path: Path,
            *,
            blocking: bool,
            namespace: str,
        ) -> Iterator[bool]:
            nonlocal claim_count
            del target_path, blocking, namespace
            with claim_counter_lock:
                call_index = claim_count
                claim_count += 1
            if call_index == 0:
                waiting.set()
                if not release.wait(timeout=5):
                    raise AssertionError("timed out releasing external claim")
            yield True

        monkeypatch.setattr(artifacts_module, "path_file_claim", controlled_claim)

        def take_claim(entered: threading.Event | None = None) -> None:
            try:
                with store._write_claim():
                    if entered is not None:
                        entered.set()
            except BaseException as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
                errors.append(exc)

        first = threading.Thread(target=take_claim)
        second = threading.Thread(target=take_claim, args=(unrelated_claimed,))
        first.start()
        try:
            assert waiting.wait(timeout=5)
            second.start()
            assert unrelated_claimed.wait(timeout=5)
        finally:
            release.set()
            first.join(timeout=5)
            if second.ident is not None:
                second.join(timeout=5)

        assert not first.is_alive()
        assert not second.is_alive()
        assert errors == []

    def test_cleanup_token_is_invalidated_by_cross_process_put(self, tmp_path):
        payload = b"cross-process token"
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        receipt = store.put_with_cleanup_token(payload)
        assert receipt.created is True
        assert receipt.cleanup_token is not None

        script = """
import sys
from easycat.runtime.artifacts import FilesystemArtifactStore

store = FilesystemArtifactStore("sess", data_dir=sys.argv[1])
print(store.put(b"cross-process token"), flush=True)
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            check=False,
            capture_output=True,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    filter(
                        None,
                        (
                            str(Path(__file__).parents[2] / "src"),
                            os.environ.get("PYTHONPATH"),
                        ),
                    )
                ),
            },
            text=True,
            timeout=5,
        )

        assert completed.returncode == 0, (
            f"stdout: {completed.stdout!r}\nstderr: {completed.stderr!r}"
        )
        assert completed.stdout.strip() == receipt.ref
        assert store.delete_if_cleanup_token(receipt.ref, receipt.cleanup_token) is False
        assert store.get(receipt.ref) == payload

    def test_cleanup_token_deletes_when_no_later_put_exists(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        receipt = store.put_with_cleanup_token(b"owned")
        assert receipt.cleanup_token is not None

        assert store.delete_if_cleanup_token(receipt.ref, receipt.cleanup_token) is True
        assert not store.has(receipt.ref)

    def test_first_journal_epoch_adopts_artifact_staged_before_journal(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        receipt = store.put_with_cleanup_token(b"staged before journal")

        assert store.begin_journal_epoch(set()) == 0
        assert store.has(receipt.ref)
        assert store.delete_if_cleanup_token(receipt.ref, receipt.cleanup_token) is True

    def test_bound_artifact_survives_duplicate_put_but_reclaims_next_epoch(self, tmp_path):
        first = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=32)
        second = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=32)
        first.begin_journal_epoch(set())
        receipt = first.put_with_cleanup_token(b"managed duplicate")

        assert second.put(b"managed duplicate") == receipt.ref
        assert first.delete_if_cleanup_token(receipt.ref, receipt.cleanup_token) is False
        assert first.begin_journal_epoch(set()) == 1
        assert first.has(receipt.ref) is False
        assert first._current_bytes == 0

    def test_committed_ref_is_adopted_and_revokes_cleanup_ownership(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        store.begin_journal_epoch(set())
        receipt = store.put_with_cleanup_token(b"committed artifact")

        assert store.begin_journal_epoch({receipt.ref}) == 0
        assert store.has(receipt.ref)
        assert store.delete_if_cleanup_token(receipt.ref, receipt.cleanup_token) is False

        assert store.begin_journal_epoch(set()) == 1
        assert store.has(receipt.ref) is False

    def test_journal_retirement_seals_only_the_prior_epoch(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        store.begin_journal_epoch(set())
        retiring_ref = store.put(b"retiring")
        retired_only_ref = store.put(b"retired only")

        assert store._prepare_journal_retirement() is True
        replacement_ref = store.put(b"replacement")
        duplicate_ref = store.put(b"retiring")
        assert duplicate_ref == retiring_ref

        assert store._complete_journal_retirement() is True
        assert store.get(retiring_ref) == b"retiring"
        assert store.has(retired_only_ref) is False
        assert store.get(replacement_ref) == b"replacement"

    def test_journal_retirement_preserves_unbound_artifacts(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        unbound_ref = store.put(b"before journal")

        assert store._prepare_journal_retirement() is True
        assert store._complete_journal_retirement() is True
        assert store.get(unbound_ref) == b"before journal"

    def test_journal_retirement_replays_interrupted_accounting(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        store.begin_journal_epoch(set())
        refs = {store.put(b"first old blob"), store.put(b"second old blob")}
        assert store._prepare_journal_retirement() is True
        original_complete = FilesystemArtifactStore._complete_pending_delete_locked
        failed = False

        def fail_once(self, pending):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("injected interrupted delete accounting")
            return original_complete(self, pending)

        monkeypatch.setattr(
            FilesystemArtifactStore,
            "_complete_pending_delete_locked",
            fail_once,
        )
        assert store._complete_journal_retirement() is False
        assert FilesystemArtifactStore._pending_journal_retirements(tmp_path) == ("sess",)

        monkeypatch.setattr(
            FilesystemArtifactStore,
            "_complete_pending_delete_locked",
            original_complete,
        )
        assert store._complete_journal_retirement() is True
        assert all(store.has(ref) is False for ref in refs)
        assert store._current_bytes == 0
        assert FilesystemArtifactStore._pending_journal_retirements(tmp_path) == ()

    def test_invalid_journal_retirement_intent_preserves_artifacts(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        store.begin_journal_epoch(set())
        ref = store.put(b"preserve on invalid intent")
        marker = store._dir / ".easycat-artifact-retirement-v1.json"
        marker.write_text('{"replacement_epoch":"bad"}\n', encoding="ascii")

        assert store._complete_journal_retirement() is False
        assert store.get(ref) == b"preserve on invalid intent"

    def test_put_uses_replacement_epoch_after_interrupted_prepare(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        store.begin_journal_epoch(set())
        retiring_ref = store.put(b"old session")
        original_write_epoch = FilesystemArtifactStore._write_artifact_epoch_locked

        def fail_epoch_write(self, epoch: str) -> None:
            raise OSError("interrupted rotation")

        monkeypatch.setattr(
            FilesystemArtifactStore,
            "_write_artifact_epoch_locked",
            fail_epoch_write,
        )
        assert store._prepare_journal_retirement() is False

        monkeypatch.setattr(
            FilesystemArtifactStore,
            "_write_artifact_epoch_locked",
            original_write_epoch,
        )
        replacement_ref = store.put(b"new session")
        assert store._complete_journal_retirement() is True
        assert store.has(retiring_ref) is False
        assert store.get(replacement_ref) == b"new session"

    def test_journal_retirement_retries_blob_unlink_failure(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        if not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("requires descriptor artifact deletion")
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        store.begin_journal_epoch(set())
        retiring_ref = store.put(b"cannot unlink yet")
        assert store._prepare_journal_retirement() is True
        original_delete_name = FilesystemArtifactStore._delete_name

        def fail_blob_unlink(self, directory_fd: int, name: str) -> None:
            if name.endswith(".bin"):
                return
            original_delete_name(self, directory_fd, name)

        monkeypatch.setattr(
            FilesystemArtifactStore,
            "_delete_name",
            fail_blob_unlink,
        )
        assert store._complete_journal_retirement() is False
        assert store.has(retiring_ref)
        assert FilesystemArtifactStore._pending_journal_retirements(tmp_path) == ("sess",)
        store.close()

        monkeypatch.setattr(
            FilesystemArtifactStore,
            "_delete_name",
            original_delete_name,
        )
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        assert store._complete_journal_retirement() is True
        assert store.has(retiring_ref) is False
        assert store._current_bytes == 0
        assert FilesystemArtifactStore._pending_journal_retirements(tmp_path) == ()

    def test_fallback_rejects_hardlinked_retirement_intent(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        store.begin_journal_epoch(set())
        ref = store.put(b"retiring but protected")
        assert store._prepare_journal_retirement() is True
        marker = store._dir / ".easycat-artifact-retirement-v1.json"
        try:
            os.link(marker, tmp_path / "retirement-link")
        except OSError:
            pytest.skip("hard links are unavailable in this test environment")
        monkeypatch.setattr(artifacts_module, "_SUPPORTS_DESCRIPTOR_ARTIFACT_IO", False)

        assert store._complete_journal_retirement() is False
        assert store.get(ref) == b"retiring but protected"

    def test_fallback_rejects_hardlinked_retiring_epoch(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        store.begin_journal_epoch(set())
        ref = store.put(b"epoch selector protected")
        epoch = store._dir / ".easycat-artifact-epoch-v1.json"
        try:
            os.link(epoch, tmp_path / "epoch-link")
        except OSError:
            pytest.skip("hard links are unavailable in this test environment")
        monkeypatch.setattr(artifacts_module, "_SUPPORTS_DESCRIPTOR_ARTIFACT_IO", False)

        assert store._prepare_journal_retirement() is False
        assert store.get(ref) == b"epoch selector protected"

    def test_duplicate_put_revokes_cleanup_ownership(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        first = store.put_with_cleanup_token(b"shared")
        assert first.cleanup_token is not None
        token_path = store._cleanup_token_path(first.ref)
        assert token_path.exists()

        second = store.put_with_cleanup_token(b"shared")

        assert second.ref == first.ref
        assert second.created is False
        assert second.cleanup_token is None
        assert not token_path.exists()
        assert store.delete_if_cleanup_token(first.ref, first.cleanup_token) is False
        assert store.get(first.ref) == b"shared"

    @pytest.mark.parametrize("use_fallback", [False, True])
    def test_duplicate_put_fails_when_cleanup_ownership_cannot_be_revoked(
        self,
        tmp_path,
        monkeypatch,
        use_fallback,
    ):
        if not use_fallback and not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        if use_fallback:
            monkeypatch.setattr(artifacts_module, "_SUPPORTS_DESCRIPTOR_ARTIFACT_IO", False)
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        first = store.put_with_cleanup_token(b"shared")
        assert first.cleanup_token is not None
        token_path = store._cleanup_token_path(first.ref)
        original_unlink = artifacts_module.os.unlink

        def deny_token_unlink(path, *args, **kwargs):
            if str(path).endswith(".token"):
                raise PermissionError(str(path))
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(artifacts_module.os, "unlink", deny_token_unlink)

        failed = store.put_with_cleanup_token(b"shared")

        assert failed.ref == ""
        assert failed.created is False
        assert failed.cleanup_token is None
        assert token_path.read_text(encoding="ascii") == first.cleanup_token
        assert store.get(first.ref) == b"shared"

    @pytest.mark.parametrize("use_fallback", [False, True])
    def test_cleanup_token_creation_does_not_use_temp_rename(
        self,
        tmp_path,
        monkeypatch,
        use_fallback,
    ):
        if not use_fallback and not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        if use_fallback:
            monkeypatch.setattr(artifacts_module, "_SUPPORTS_DESCRIPTOR_ARTIFACT_IO", False)
        destinations: list[object] = []
        original_replace = artifacts_module.os.replace

        def track_replace(source, destination, *args, **kwargs):
            destinations.append(destination)
            return original_replace(source, destination, *args, **kwargs)

        monkeypatch.setattr(artifacts_module.os, "replace", track_replace)
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)

        receipt = store.put_with_cleanup_token(b"direct token creation")

        assert receipt.ref
        assert receipt.created is True
        assert receipt.cleanup_token is not None
        assert not any(str(destination).endswith(".token") for destination in destinations)
        token_path = store._cleanup_token_path(receipt.ref)
        assert token_path.read_text(encoding="ascii") == receipt.cleanup_token
        assert token_path.stat().st_mode & 0o777 == 0o600

    @pytest.mark.parametrize("use_fallback", [False, True])
    def test_new_blob_replaces_stale_cleanup_capability(
        self,
        tmp_path,
        monkeypatch,
        use_fallback,
    ):
        if not use_fallback and not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        if use_fallback:
            monkeypatch.setattr(artifacts_module, "_SUPPORTS_DESCRIPTOR_ARTIFACT_IO", False)
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        first = store.put_with_cleanup_token(b"recreated")
        assert first.cleanup_token is not None
        store._ref_path(first.ref).unlink()

        second = store.put_with_cleanup_token(b"recreated")

        assert second.ref == first.ref
        assert second.created is True
        assert second.cleanup_token is not None
        assert second.cleanup_token != first.cleanup_token
        assert store.delete_if_cleanup_token(first.ref, first.cleanup_token) is False
        assert store.delete_if_cleanup_token(second.ref, second.cleanup_token) is True

    @pytest.mark.parametrize("use_fallback", [False, True])
    def test_partial_cleanup_token_creation_fails_closed(
        self,
        tmp_path,
        monkeypatch,
        use_fallback,
    ):
        if not use_fallback and not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        if use_fallback:
            monkeypatch.setattr(artifacts_module, "_SUPPORTS_DESCRIPTOR_ARTIFACT_IO", False)
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        original_write_all = artifacts_module._write_all_fd

        def fail_after_partial_write(fd: int, payload: bytes) -> None:
            if len(payload) != 32:
                original_write_all(fd, payload)
                return
            os.write(fd, payload[: len(payload) // 2])
            raise OSError(errno.ENOSPC, "partial token write")

        monkeypatch.setattr(artifacts_module, "_write_all_fd", fail_after_partial_write)

        failed = store.put_with_cleanup_token(b"new artifact")

        assert failed.ref == ""
        ref = hashlib.sha256(b"new artifact").hexdigest()
        assert store.has(ref) is False
        assert store._cleanup_token_path(ref).exists() is False

    @pytest.mark.parametrize("use_fallback", [False, True])
    def test_duplicate_revokes_hardlinked_token_without_modifying_external_link(
        self,
        tmp_path,
        monkeypatch,
        use_fallback,
    ):
        if not use_fallback and not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        if use_fallback:
            monkeypatch.setattr(artifacts_module, "_SUPPORTS_DESCRIPTOR_ARTIFACT_IO", False)
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        first = store.put_with_cleanup_token(b"shared")
        assert first.cleanup_token is not None
        token_path = store._cleanup_token_path(first.ref)
        external = tmp_path / "outside-token"
        try:
            os.link(token_path, external)
        except OSError:
            pytest.skip("hard links are unavailable in this test environment")
        original = external.read_bytes()
        assert store.delete_if_cleanup_token(first.ref, first.cleanup_token) is False

        second = store.put_with_cleanup_token(b"shared")

        assert second.ref == first.ref
        assert second.created is False
        assert second.cleanup_token is None
        assert token_path.exists() is False
        assert external.read_bytes() == original
        assert store.delete_if_cleanup_token(first.ref, first.cleanup_token) is False
        assert store.get(first.ref) == b"shared"

    @pytest.mark.parametrize("use_fallback", [False, True])
    def test_duplicate_revokes_symlinked_token_without_touching_target(
        self,
        tmp_path,
        monkeypatch,
        use_fallback,
    ):
        if not use_fallback and not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        if use_fallback:
            monkeypatch.setattr(artifacts_module, "_SUPPORTS_DESCRIPTOR_ARTIFACT_IO", False)
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        first = store.put_with_cleanup_token(b"shared")
        assert first.cleanup_token is not None
        token_path = store._cleanup_token_path(first.ref)
        token_path.unlink()
        target = tmp_path / "outside-token-target"
        target.write_bytes(b"do not modify")
        try:
            token_path.symlink_to(target)
        except OSError:
            pytest.skip("symlinks are unavailable in this test environment")

        second = store.put_with_cleanup_token(b"shared")

        assert second.ref == first.ref
        assert second.created is False
        assert second.cleanup_token is None
        assert token_path.exists() is False
        assert token_path.is_symlink() is False
        assert target.read_bytes() == b"do not modify"
        assert store.delete_if_cleanup_token(first.ref, first.cleanup_token) is False
        assert store.get(first.ref) == b"shared"

    def test_legacy_raw_cleanup_token_remains_readable(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        receipt = store.put_with_cleanup_token(b"legacy")
        assert receipt.cleanup_token is not None
        store._cleanup_token_path(receipt.ref).write_text(
            receipt.cleanup_token,
            encoding="ascii",
        )

        assert store.delete_if_cleanup_token(receipt.ref, receipt.cleanup_token) is True

    def test_unknown_cleanup_token_format_fails_closed(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        receipt = store.put_with_cleanup_token(b"unknown token")
        assert receipt.cleanup_token is not None
        store._cleanup_token_path(receipt.ref).write_bytes(b'{"version":2}')

        assert store.delete_if_cleanup_token(receipt.ref, receipt.cleanup_token) is False
        assert store.get(receipt.ref) == b"unknown token"

    def test_path_fallback_remains_usable_without_descriptor_relative_io(
        self,
        tmp_path,
        monkeypatch,
    ):
        session_dir = tmp_path / "artifacts" / "sess"
        session_dir.mkdir(parents=True, mode=0o777)
        os.chmod(session_dir, 0o777)
        monkeypatch.setattr(artifacts_module, "_SUPPORTS_DESCRIPTOR_ARTIFACT_IO", False)
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)

        ref = store.put(b"portable artifact")

        assert ref
        assert store.has(ref)
        assert store.get(ref) == b"portable artifact"
        assert store.get_head_tail(ref, byte_cap=4) == b"portfact"
        if os.name != "nt":
            assert session_dir.stat().st_mode & 0o777 == 0o700
            assert store._accounting_path().stat().st_mode & 0o777 == 0o600
        store.delete(ref)
        assert store.has(ref) is False

    def test_path_fallback_rejects_symlinked_shard(self, tmp_path, monkeypatch):
        monkeypatch.setattr(artifacts_module, "_SUPPORTS_DESCRIPTOR_ARTIFACT_IO", False)
        payload = b"portable artifact"
        ref = hashlib.sha256(payload).hexdigest()
        session_dir = tmp_path / "artifacts" / "sess"
        session_dir.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        try:
            (session_dir / ref[:2]).symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are unavailable in this test environment")

        store = FilesystemArtifactStore("sess", data_dir=tmp_path)

        assert store.put(payload) == ""
        assert not (outside / f"{ref}.bin").exists()

    def test_path_fallback_detects_windows_reparse_points(self, tmp_path, monkeypatch):
        reparse_flag = getattr(artifacts_module.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if not reparse_flag:
            pytest.skip("reparse-point metadata is unavailable")
        junction = tmp_path / "junction"
        junction.mkdir()
        real_lstat = type(junction).lstat

        def lstat_with_reparse(path):
            if path == junction:
                return SimpleNamespace(
                    st_mode=artifacts_module.stat.S_IFDIR,
                    st_file_attributes=reparse_flag,
                )
            return real_lstat(path)

        monkeypatch.setattr(type(junction), "lstat", lstat_with_reparse)

        assert artifacts_module._path_is_link_or_reparse(junction)

    def test_path_fallback_scan_skips_descendant_windows_reparse_directory(
        self,
        tmp_path,
        monkeypatch,
    ):
        reparse_flag = getattr(artifacts_module.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if not reparse_flag:
            pytest.skip("reparse-point metadata is unavailable")
        session_dir = tmp_path / "artifacts" / "sess"
        junction = session_dir / "junction"
        junction.mkdir(parents=True)
        (junction / "ignored.bin").write_bytes(b"outside")
        original_scandir = artifacts_module.os.scandir

        class ReparseEntry:
            def __init__(self, entry):
                self._entry = entry
                self.name = entry.name
                self.path = entry.path

            def stat(self, *, follow_symlinks=True):
                metadata = self._entry.stat(follow_symlinks=follow_symlinks)
                return SimpleNamespace(
                    st_mode=metadata.st_mode,
                    st_file_attributes=reparse_flag,
                )

        class ReparseScandir:
            def __init__(self, entries):
                self._entries = entries

            def __enter__(self):
                self._entries.__enter__()
                return self

            def __iter__(self):
                return iter(
                    ReparseEntry(entry) if entry.name == junction.name else entry
                    for entry in self._entries
                )

            def __exit__(self, *args):
                return self._entries.__exit__(*args)

        def marked_scandir(path):
            entries = original_scandir(path)
            if Path(path) == session_dir:
                return ReparseScandir(entries)
            return entries

        monkeypatch.setattr(artifacts_module, "_SUPPORTS_DESCRIPTOR_ARTIFACT_IO", False)
        monkeypatch.setattr(artifacts_module.os, "scandir", marked_scandir)

        store = FilesystemArtifactStore("sess", data_dir=tmp_path)

        assert store._current_bytes == 0

    def test_path_fallback_treats_metadata_errors_as_unsafe(self, tmp_path, monkeypatch):
        guarded = tmp_path / "guarded"
        guarded.mkdir()
        real_lstat = type(guarded).lstat

        def denied_lstat(path):
            if path == guarded:
                raise PermissionError(str(path))
            return real_lstat(path)

        monkeypatch.setattr(type(guarded), "lstat", denied_lstat)

        assert artifacts_module._path_is_link_or_reparse(guarded)

    def test_file_created(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        ref = store.put(b"data")
        path = tmp_path / "artifacts" / "sess" / ref[:2] / f"{ref}.bin"
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

    def test_delete_rejects_path_traversal_refs(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        ref = store.put(b"legitimate")
        victim = tmp_path / "victim.bin"
        victim.write_bytes(b"keep me")
        stored_bytes = store._current_bytes

        store.delete("../victim")

        assert victim.read_bytes() == b"keep me"
        assert store.get(ref) == b"legitimate"
        assert store._current_bytes == stored_bytes

    def test_get_missing_returns_none(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        assert store.get("nonexistent") is None

    def test_symlinked_blob_is_not_read_or_treated_as_a_dedup_hit(self, tmp_path):
        """Artifact refs must never follow an injected symlink outside the store."""
        payload = b"trusted payload"
        ref = hashlib.sha256(payload).hexdigest()
        shard = tmp_path / "artifacts" / "sess" / ref[:2]
        shard.mkdir(parents=True)
        victim = tmp_path / "outside.bin"
        victim.write_bytes(b"outside data")
        artifact_path = shard / f"{ref}.bin"
        try:
            artifact_path.symlink_to(victim)
        except OSError:
            pytest.skip("symlinks are unavailable in this test environment")

        store = FilesystemArtifactStore("sess", data_dir=tmp_path)

        assert store.get(ref) is None
        assert store.has(ref) is False
        assert store.put(payload) == ref
        assert artifact_path.is_symlink() is False
        assert artifact_path.read_bytes() == payload
        assert victim.read_bytes() == b"outside data"

    def test_shard_symlink_is_not_followed_for_writes(self, tmp_path):
        """Writes must not escape through an injected shard-directory symlink."""
        payload = b"trusted payload"
        ref = hashlib.sha256(payload).hexdigest()
        session_dir = tmp_path / "artifacts" / "sess"
        session_dir.mkdir(parents=True)
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        shard = session_dir / ref[:2]
        try:
            shard.symlink_to(outside_dir, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are unavailable in this test environment")

        store = FilesystemArtifactStore("sess", data_dir=tmp_path)

        assert store.put(payload) == ""
        assert not (outside_dir / f"{ref}.bin").exists()

    def test_session_symlink_is_not_followed_for_writes(self, tmp_path):
        """Writes must not escape through an injected session-directory symlink."""
        payload = b"trusted payload"
        ref = hashlib.sha256(payload).hexdigest()
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        session_dir = artifacts_dir / "sess"
        try:
            session_dir.symlink_to(outside_dir, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are unavailable in this test environment")

        store = FilesystemArtifactStore("sess", data_dir=tmp_path)

        assert store.put(payload) == ""
        assert not (outside_dir / ref[:2] / f"{ref}.bin").exists()

    def test_artifact_root_symlink_is_not_followed_for_writes(self, tmp_path):
        """Writes must not escape through an injected artifacts-root symlink."""
        payload = b"trusted payload"
        ref = hashlib.sha256(payload).hexdigest()
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        artifacts_dir = tmp_path / "artifacts"
        try:
            artifacts_dir.symlink_to(outside_dir, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are unavailable in this test environment")

        store = FilesystemArtifactStore("sess", data_dir=tmp_path)

        assert store.put(payload) == ""
        assert not (outside_dir / "sess" / ref[:2] / f"{ref}.bin").exists()

    def test_artifacts_ancestor_symlink_is_not_followed_for_writes(self, tmp_path):
        payload = b"trusted payload"
        artifacts_dir = tmp_path / "artifacts"
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        try:
            artifacts_dir.symlink_to(outside_dir, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are unavailable in this test environment")

        store = FilesystemArtifactStore("sess", data_dir=tmp_path)

        assert store.put(payload) == ""
        assert not (outside_dir / "sess").exists()

    def test_artifact_root_symlink_is_not_read_or_deleted(self, tmp_path):
        """Parent links must not expose or delete blobs outside the store."""
        payload = b"outside payload"
        ref = hashlib.sha256(payload).hexdigest()
        outside_dir = tmp_path / "outside"
        outside_path = outside_dir / "sess" / ref[:2] / f"{ref}.bin"
        outside_path.parent.mkdir(parents=True)
        outside_path.write_bytes(payload)
        artifacts_dir = tmp_path / "artifacts"
        try:
            artifacts_dir.symlink_to(outside_dir, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are unavailable in this test environment")

        store = FilesystemArtifactStore("sess", data_dir=tmp_path)

        assert store._current_bytes == 0
        assert store.get(ref) is None
        assert store.has(ref) is False
        store.delete(ref)
        assert outside_path.read_bytes() == payload

    def test_precreated_temp_symlink_cannot_overwrite_external_file(self, tmp_path):
        payload = b"trusted payload"
        ref = hashlib.sha256(payload).hexdigest()
        shard = tmp_path / "artifacts" / "sess" / ref[:2]
        shard.mkdir(parents=True)
        victim = tmp_path / "victim.bin"
        victim.write_bytes(b"keep me")
        legacy_tmp = shard / f"{ref}.tmp"
        try:
            legacy_tmp.symlink_to(victim)
        except OSError:
            pytest.skip("symlinks are unavailable in this test environment")

        store = FilesystemArtifactStore("sess", data_dir=tmp_path)

        assert store.put(payload) == ref
        assert victim.read_bytes() == b"keep me"
        assert (shard / f"{ref}.bin").read_bytes() == payload

    def test_artifact_candidates_are_opened_nonblocking(self, monkeypatch):
        nonblocking = getattr(os, "O_NONBLOCK", 0)
        if not nonblocking:
            pytest.skip("O_NONBLOCK is unavailable")
        seen_flags = 0

        def fail_open(name, flags, **kwargs):
            nonlocal seen_flags
            seen_flags = flags
            raise OSError(errno.ENXIO, str(name))

        monkeypatch.setattr(artifacts_module.os, "open", fail_open)

        with pytest.raises(OSError):
            artifacts_module._open_regular_at(1, "fifo.bin")

        assert seen_flags & nonblocking

    def test_artifact_candidates_are_opened_in_binary_mode_when_available(self, monkeypatch):
        binary = 1 << 28
        seen_flags = 0

        def fail_open(name, flags, **kwargs):
            nonlocal seen_flags
            seen_flags = flags
            raise OSError(errno.ENXIO, str(name))

        monkeypatch.setattr(os, "O_BINARY", binary, raising=False)
        flags = artifacts_module._artifact_file_open_flags()
        write_flags = artifacts_module._artifact_file_write_flags()
        accounting_flags = artifacts_module._artifact_accounting_open_flags()
        monkeypatch.setattr(artifacts_module, "_FILE_OPEN_FLAGS", flags)
        monkeypatch.setattr(artifacts_module.os, "open", fail_open)

        with pytest.raises(OSError):
            artifacts_module._open_regular_at(1, "binary.bin")

        assert flags & binary
        assert write_flags & binary
        assert accounting_flags & binary
        assert seen_flags & binary

    def test_put_reuses_one_validated_session_descriptor(self, tmp_path, monkeypatch):
        if not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        opened: list[tuple[Path, bool]] = []
        original_open_directory_chain = artifacts_module._open_directory_chain

        def count_open_directory_chain(path: Path, *, create: bool) -> int:
            opened.append((path, create))
            return original_open_directory_chain(path, create=create)

        monkeypatch.setattr(
            artifacts_module,
            "_open_directory_chain",
            count_open_directory_chain,
        )

        assert store.put(b"one descriptor")

        assert opened == [
            (store._artifacts_dir, True),
            (store._dir, True),
        ]

    def test_delete_reuses_one_validated_session_descriptor(self, tmp_path, monkeypatch):
        if not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        ref = store.put(b"one delete descriptor")
        opened: list[tuple[Path, bool]] = []
        original_open_directory_chain = artifacts_module._open_directory_chain

        def count_open_directory_chain(path: Path, *, create: bool) -> int:
            opened.append((path, create))
            return original_open_directory_chain(path, create=create)

        monkeypatch.setattr(
            artifacts_module,
            "_open_directory_chain",
            count_open_directory_chain,
        )

        store.delete(ref)

        assert opened == [
            (store._artifacts_dir, True),
            (store._dir, True),
        ]

    def test_conditional_delete_reuses_one_validated_session_descriptor(
        self,
        tmp_path,
        monkeypatch,
    ):
        if not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        receipt = store.put_with_cleanup_token(b"one conditional delete descriptor")
        assert receipt.cleanup_token is not None
        opened: list[tuple[Path, bool]] = []
        original_open_directory_chain = artifacts_module._open_directory_chain

        def count_open_directory_chain(path: Path, *, create: bool) -> int:
            opened.append((path, create))
            return original_open_directory_chain(path, create=create)

        monkeypatch.setattr(
            artifacts_module,
            "_open_directory_chain",
            count_open_directory_chain,
        )

        assert store.delete_if_cleanup_token(receipt.ref, receipt.cleanup_token)

        assert opened == [
            (store._artifacts_dir, True),
            (store._dir, True),
        ]

    def test_reused_session_descriptor_is_thread_local(self, tmp_path, monkeypatch):
        if not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        ref = store.put(b"thread local descriptor")
        worker_opened = threading.Event()
        original_open_directory_chain = artifacts_module._open_directory_chain

        def track_open_directory_chain(path: Path, *, create: bool) -> int:
            if threading.current_thread() is not threading.main_thread():
                worker_opened.set()
            return original_open_directory_chain(path, create=create)

        monkeypatch.setattr(
            artifacts_module,
            "_open_directory_chain",
            track_open_directory_chain,
        )

        with store._write_claim(), store._reuse_session_fd_locked(create=True):
            worker = threading.Thread(target=store.has, args=(ref,))
            worker.start()
            worker.join(timeout=5)

        assert not worker.is_alive()
        assert worker_opened.is_set()

    def test_failed_partial_write_removes_temporary_file(self, tmp_path, monkeypatch):
        def fail_after_partial_write(fd: int, payload: bytes) -> None:
            os.write(fd, payload[:1])
            raise OSError(errno.ENOSPC, "disk full")

        monkeypatch.setattr(artifacts_module, "_write_all_fd", fail_after_partial_write)
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)

        assert store.put(b"partial payload") == ""

        session_dir = tmp_path / "artifacts" / "sess"
        assert not list(session_dir.rglob("*.tmp"))

    def test_reading_existing_shard_does_not_chmod_it(self, tmp_path, monkeypatch):
        if not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        payload = b"read-only artifact"
        ref = store.put(payload)

        def fail_fchmod(fd: int, mode: int) -> None:
            raise OSError(errno.EROFS, "read-only filesystem")

        monkeypatch.setattr(artifacts_module.os, "fchmod", fail_fchmod)

        assert store.has(ref)
        assert store.get(ref) == payload

    def test_get_head_tail_reads_bounded_window(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        payload = b"a" * 10 + b"middle" * 20 + b"z" * 10
        ref = store.put(payload)
        assert store.get_head_tail(ref, byte_cap=10) == b"a" * 10 + b"z" * 10

    def test_get_head_tail_returns_small_payloads_unchanged(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        ref = store.put(b"small")
        assert store.get_head_tail(ref, byte_cap=10) == b"small"

    def test_get_head_tail_treats_unsupported_io_as_missing(self, tmp_path, monkeypatch):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)

        def unsupported(_ref: str) -> int:
            raise NotImplementedError

        monkeypatch.setattr(store, "_open_ref_fd", unsupported)

        assert store.get_head_tail("0" * 64, byte_cap=10) is None

    def test_permissions(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path)
        ref = store.put(b"secret data")
        path = tmp_path / "artifacts" / "sess" / ref[:2] / f"{ref}.bin"
        accounting_path = tmp_path / "artifacts" / "sess" / artifacts_module._ACCOUNTING_FILENAME
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert accounting_path.stat().st_mode & 0o777 == 0o600

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

    def test_repeated_cap_refusals_reuse_the_reconciled_total(self, tmp_path, monkeypatch):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        ref = store.put(b"a" * 10)
        scans = 0
        writes = 0
        original_scan = store._stored_bytes
        original_write = store._write_accounting_locked

        def count_scan() -> int:
            nonlocal scans
            scans += 1
            return original_scan()

        def count_write(accounting):
            nonlocal writes
            writes += 1
            return original_write(accounting)

        monkeypatch.setattr(store, "_stored_bytes", count_scan)
        monkeypatch.setattr(store, "_write_accounting_locked", count_write)

        assert store.put(b"b") == ""
        assert store.put(b"c") == ""
        assert scans == 1
        assert writes == 0

        FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10).delete(ref)

        assert store.put(b"d")
        assert scans == 1
        assert writes == 1

    def test_two_stale_instances_share_one_byte_cap(self, tmp_path):
        first = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        second = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)

        assert first.put(b"a" * 6)
        assert second.put(b"b" * 6) == ""

        reopened = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        assert reopened._current_bytes == 6

    def test_cap_rejection_cache_uses_accounting_revision_to_avoid_aba(
        self,
        tmp_path,
        monkeypatch,
    ):
        first = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        second = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        old_ref = first.put(b"a" * 10)
        assert old_ref
        assert first.put(b"over-cap") == ""
        cached = first._cap_rejected_accounting
        assert cached is not None
        assert cached[1] == 10

        second.delete(old_ref)
        monkeypatch.setattr(second, "_put_new_locked", lambda _ref, _payload: False)
        assert second.put(b"b" * 10) == ""

        stranded = artifacts_module._ArtifactAccounting.from_bytes(
            second._accounting_path().read_bytes()
        )
        assert stranded.total_bytes == 10
        assert stranded.revision > cached[0]
        assert second._stored_bytes() == 0

        assert first.put(b"c" * 5)
        assert first._current_bytes == 5

    def test_accounting_revision_increments_for_transitions_and_reconciliation(
        self,
        tmp_path,
    ):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        ref = store.put(b"a" * 6)
        after_put = artifacts_module._ArtifactAccounting.from_bytes(
            store._accounting_path().read_bytes()
        )

        with store._write_claim():
            accounting = store._load_accounting_locked(persist_missing=False)
            pending = store._begin_pending_delete_locked(accounting, ref, 6)
            assert pending.revision == after_put.revision + 1
            store._delete_ref_locked(ref)
            completed = store._complete_pending_delete_locked(pending)
            assert completed.revision == pending.revision + 1

        reopened = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        reconciled = artifacts_module._ArtifactAccounting.from_bytes(
            reopened._accounting_path().read_bytes()
        )

        assert reconciled.revision == completed.revision + 1
        assert reconciled.total_bytes == 0

    def test_stale_delete_does_not_undercount_another_instances_put(self, tmp_path):
        seed = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        old_ref = seed.put(b"a" * 6)
        first = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        stale = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)

        assert first.put(b"b" * 4)
        stale.delete(old_ref)

        assert stale._current_bytes == 4
        assert stale.put(b"c" * 7) == ""

    def test_concurrent_processes_share_one_byte_cap(self, tmp_path):  # noqa: C901
        script = """
import sys
from easycat.runtime.artifacts import FilesystemArtifactStore

store = FilesystemArtifactStore("sess", data_dir=sys.argv[1], max_bytes=10)
print("ready", flush=True)
sys.stdin.readline()
print(store.put(sys.argv[2].encode("ascii")), flush=True)
"""
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(tmp_path), payload],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    **os.environ,
                    "PYTHONPATH": os.pathsep.join(
                        filter(
                            None,
                            (
                                str(Path(__file__).parents[2] / "src"),
                                os.environ.get("PYTHONPATH"),
                            ),
                        )
                    ),
                },
                text=True,
            )
            for payload in ("a" * 6, "b" * 6)
        ]
        ready_lines: queue.Queue[tuple[int, str]] = queue.Queue()
        ready_threads: list[threading.Thread] = []
        communicated: set[int] = set()

        def read_ready(index: int, process: subprocess.Popen[str]) -> None:
            assert process.stdout is not None
            ready_lines.put((index, process.stdout.readline().strip()))

        for index, process in enumerate(processes):
            thread = threading.Thread(
                target=read_ready,
                args=(index, process),
                daemon=True,
            )
            thread.start()
            ready_threads.append(thread)
        try:
            deadline = time.monotonic() + 5
            readiness: dict[int, str] = {}
            while len(readiness) < len(processes):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    pytest.fail("timed out waiting for artifact worker readiness")
                try:
                    index, line = ready_lines.get(timeout=remaining)
                except queue.Empty:
                    pytest.fail("timed out waiting for artifact worker readiness")
                readiness[index] = line
            assert readiness == {0: "ready", 1: "ready"}
            for process in processes:
                assert process.stdin is not None
                process.stdin.write("\n")
                process.stdin.close()
                process.stdin = None
            results: list[str] = []
            for index, process in enumerate(processes):
                stdout, stderr = process.communicate(timeout=5)
                communicated.add(index)
                assert process.returncode == 0, f"stdout: {stdout!r}\nstderr: {stderr!r}"
                results.append(stdout.strip())
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
            for thread in ready_threads:
                thread.join(timeout=5)
            for index, process in enumerate(processes):
                if index not in communicated:
                    process.communicate(timeout=5)

        assert sum(bool(result) for result in results) == 1
        reopened = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        assert reopened._current_bytes == 6

    @pytest.mark.parametrize("publish_blob", [False, True])
    def test_reserved_put_is_reclaimed_under_cap_pressure(self, tmp_path, publish_blob):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        payload = b"a" * 6
        ref = hashlib.sha256(payload).hexdigest()
        with store._write_claim():
            accounting = store._load_accounting_locked(persist_missing=False)
            assert store._reserve_new_payload_locked(
                accounting,
                payload_size=len(payload),
            )
            if publish_blob:
                assert store._put_new_locked(ref, payload)

        reopened = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)

        assert reopened._current_bytes == (len(payload) if publish_blob else 0)
        if publish_blob:
            assert reopened.put(b"b" * 5) == ""
        else:
            assert reopened.put(b"b" * 10)

    def test_unique_put_writes_accounting_once_before_blob_publish(
        self,
        tmp_path,
        monkeypatch,
    ):
        if not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        events: list[str] = []
        original_write_accounting = store._write_accounting_locked
        original_replace = FilesystemArtifactStore._replace_file_at

        def record_accounting(accounting):
            written = original_write_accounting(accounting)
            events.append("accounting-complete")
            return written

        def record_replace(
            directory_fd: int,
            name: str,
            payload: bytes,
        ) -> None:
            if name.endswith(".bin"):
                events.append("blob-publish")
            original_replace(directory_fd, name, payload)

        monkeypatch.setattr(store, "_write_accounting_locked", record_accounting)
        monkeypatch.setattr(
            FilesystemArtifactStore,
            "_replace_file_at",
            staticmethod(record_replace),
        )

        payload = b"a" * 6
        ref = store.put(payload)
        assert ref
        assert store.put(payload) == ref

        assert events == ["accounting-complete", "blob-publish"]

    @pytest.mark.parametrize("unlink_before_crash", [False, True])
    def test_pending_delete_is_recovered_after_interruption(
        self,
        tmp_path,
        unlink_before_crash,
    ):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        ref = store.put(b"a" * 10)
        assert store.put(b"over-cap") == ""
        assert store._cap_rejected_accounting is not None
        assert store._cap_rejected_accounting[1] == 10
        with store._write_claim():
            accounting = store._load_accounting_locked(persist_missing=False)
            before_bytes = store._ref_stored_bytes_locked(ref)
            store._begin_pending_delete_locked(accounting, ref, before_bytes)
            if unlink_before_crash:
                store._delete_ref_locked(ref)

        assert store.put(b"b" * 10)
        assert store._current_bytes == 10
        assert store._cap_rejected_accounting is None
        assert not store.has(ref)

    def test_constructor_recount_retains_blob_despite_stale_pending_delete(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        ref = store.put(b"a" * 10)
        with store._write_claim():
            accounting = store._load_accounting_locked(persist_missing=False)
            before_bytes = store._ref_stored_bytes_locked(ref)
            pending = store._begin_pending_delete_locked(accounting, ref, before_bytes)
            stale_pending = store._accounting_path().read_bytes()
            store._delete_ref_locked(ref)
            store._complete_pending_delete_locked(pending)
        assert store.put(b"a" * 10) == ref
        store._accounting_path().write_bytes(stale_pending)

        reopened = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)

        assert reopened._current_bytes == 10
        assert reopened.get(ref) == b"a" * 10
        persisted = artifacts_module._ArtifactAccounting.from_bytes(
            reopened._accounting_path().read_bytes()
        )
        assert persisted.total_bytes == 10
        assert persisted.pending_delete_ref is None

    @pytest.mark.parametrize("ledger_total", [1, 10])
    def test_constructor_recounts_valid_under_or_overcounted_ledger(
        self,
        tmp_path,
        ledger_total,
    ):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        ref = store.put(b"a" * 6)
        with store._write_claim():
            store._write_accounting_locked(
                artifacts_module._ArtifactAccounting(total_bytes=ledger_total)
            )

        reopened = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)

        assert reopened._current_bytes == 6
        assert reopened.get(ref) == b"a" * 6
        persisted = artifacts_module._ArtifactAccounting.from_bytes(
            reopened._accounting_path().read_bytes()
        )
        assert persisted.total_bytes == 6

    def test_constructor_recount_repairs_low_clean_ledger_before_cap_check(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        first_ref = store.put(b"a" * 4)
        saved_low_ledger = store._accounting_path().read_bytes()
        second_ref = store.put(b"b" * 4)
        store._accounting_path().write_bytes(saved_low_ledger)

        reopened = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)

        assert reopened._current_bytes == 8
        assert reopened.has(first_ref)
        assert reopened.has(second_ref)
        assert reopened.put(b"c" * 3) == ""

    def test_constructor_metadata_error_retries_recount_before_mutation(
        self,
        tmp_path,
        monkeypatch,
    ):
        seed = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        ref = seed.put(b"a" * 6)
        with seed._write_claim():
            accounting = seed._load_accounting_locked(persist_missing=False)
            seed._write_accounting_locked(
                artifacts_module._ArtifactAccounting(
                    total_bytes=0,
                    revision=accounting.revision,
                )
            )
        original_lstat = Path.lstat
        errored = False

        def fail_once(path, *args, **kwargs):
            nonlocal errored
            if path == seed._dir and not errored:
                errored = True
                raise PermissionError(path)
            return original_lstat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", fail_once)

        reopened = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)

        assert reopened._needs_open_reconciliation is True
        assert reopened._current_bytes == 0
        assert reopened.put(b"b" * 5) == ""
        assert reopened._needs_open_reconciliation is False
        assert reopened._current_bytes == 6
        assert reopened.get(ref) == b"a" * 6

    def test_persistent_constructor_metadata_error_makes_mutations_fail_closed(
        self,
        tmp_path,
        monkeypatch,
    ):
        seed = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        ref = seed.put(b"a" * 6)
        with seed._write_claim():
            accounting = seed._load_accounting_locked(persist_missing=False)
            seed._write_accounting_locked(
                artifacts_module._ArtifactAccounting(
                    total_bytes=0,
                    revision=accounting.revision,
                )
            )
        original_lstat = Path.lstat

        def deny_session_metadata(path, *args, **kwargs):
            if path == seed._dir:
                raise PermissionError(path)
            return original_lstat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", deny_session_metadata)

        reopened = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)

        assert reopened._needs_open_reconciliation is True
        assert reopened.put(b"b" * 5) == ""
        reopened.delete(ref)
        assert reopened._needs_open_reconciliation is True
        assert reopened.get(ref) == b"a" * 6

    def test_corrupt_accounting_metadata_is_rebuilt_from_artifacts(self, tmp_path):
        first = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        ref = first.put(b"a" * 6)
        first._accounting_path().write_bytes(b"not-json")

        reopened = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)

        assert reopened._current_bytes == 6
        assert reopened.get(ref) == b"a" * 6
        assert reopened.put(b"b" * 5) == ""

    def test_valid_json_with_torn_total_and_stale_checksum_is_rebuilt(self, tmp_path):
        first = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        ref = first.put(b"a" * 6)
        torn = json.loads(first._accounting_path().read_text(encoding="ascii"))
        torn["total_bytes"] = 1
        first._accounting_path().write_text(
            json.dumps(torn, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="ascii",
        )

        reopened = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)

        assert reopened._current_bytes == 6
        assert reopened.get(ref) == b"a" * 6
        assert reopened.put(b"b" * 5) == ""

    def test_incomplete_accounting_rebuild_fails_closed(self, tmp_path, monkeypatch):
        if not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        first = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        ref = first.put(b"a" * 6)
        first._accounting_path().write_bytes(b"not-json")
        original_open = artifacts_module._open_regular_at

        def deny_artifact_open(directory_fd: int, name: str) -> int:
            if name.endswith(".bin"):
                raise PermissionError(name)
            return original_open(directory_fd, name)

        monkeypatch.setattr(artifacts_module, "_open_regular_at", deny_artifact_open)

        reopened = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)

        assert reopened.put(b"b") == ""
        assert first._accounting_path().read_bytes() == b"not-json"
        assert first._ref_path(ref).read_bytes() == b"a" * 6

    def test_unknown_accounting_version_fails_closed(self, tmp_path):
        first = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        ref = first.put(b"a" * 6)
        first._accounting_path().write_text(
            '{"future_field":true,"version":2}\n',
            encoding="ascii",
        )

        reopened = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)

        assert reopened.has(ref)
        assert reopened.put(b"b") == ""
        assert '"version":2' in first._accounting_path().read_text(encoding="ascii")

    def test_existing_accounting_updates_keep_a_stable_inode(self, tmp_path):
        if os.name == "nt":
            pytest.skip("inode identity is not portable on Windows")
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=100)
        assert store.put(b"a" * 10)
        accounting_path = store._accounting_path()
        inode = accounting_path.stat().st_ino

        assert store.put(b"b" * 10)

        assert accounting_path.stat().st_ino == inode

    def test_cached_accounting_descriptor_tracks_path_replacement(self, tmp_path):
        if not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=100)
        assert store.put(b"a" * 10)
        assert store.put(b"b" * 10)
        cached = store._accounting_fd
        assert cached is not None
        cached_fd = cached[1][0]
        accounting_path = store._accounting_path()
        old_inode = os.fstat(cached_fd).st_ino
        replacement = accounting_path.with_name("replacement-accounting")
        replacement.write_bytes(accounting_path.read_bytes())
        os.chmod(replacement, 0o600)
        os.replace(replacement, accounting_path)

        assert store.put(b"c" * 10)

        refreshed = store._accounting_fd
        assert refreshed is not None
        assert os.fstat(refreshed[1][0]).st_ino == accounting_path.stat().st_ino
        assert os.fstat(refreshed[1][0]).st_ino != old_inode

    def test_cached_accounting_descriptor_is_reused_between_puts(self, tmp_path, monkeypatch):
        if not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=100)
        assert store.put(b"a" * 10)
        assert store.put(b"b" * 10)
        cached = store._accounting_fd
        assert cached is not None
        original_open = artifacts_module.os.open

        def reject_accounting_reopen(path, *args, **kwargs):
            if path == artifacts_module._ACCOUNTING_FILENAME:
                raise AssertionError("accounting descriptor was reopened")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(artifacts_module.os, "open", reject_accounting_reopen)

        assert store.put(b"c" * 10)
        assert store._accounting_fd == cached

    def test_close_releases_cached_accounting_descriptor(self, tmp_path):
        if not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=100)
        assert store.put(b"a" * 10)
        assert store.put(b"b" * 10)
        cached = store._accounting_fd
        assert cached is not None
        cached_fd = cached[1][0]

        store.close()

        assert store._accounting_fd is None
        with pytest.raises(OSError):
            os.fstat(cached_fd)

    def test_dropped_store_releases_cached_accounting_descriptor(self, tmp_path):
        if not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=100)
        assert store.put(b"a" * 10)
        assert store.put(b"b" * 10)
        cached = store._accounting_fd
        assert cached is not None
        cached_fd = cached[1][0]
        store_ref = weakref.ref(store)

        del store
        gc.collect()

        assert store_ref() is None
        with pytest.raises(OSError):
            os.fstat(cached_fd)

    @pytest.mark.serial
    @pytest.mark.timeout(0)
    def test_fork_child_does_not_close_reused_foreign_descriptor(self, tmp_path):
        if not hasattr(os, "fork") or not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("fork and descriptor-relative artifact I/O are required")
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=100)
        assert store.put(b"a" * 10)
        assert store.put(b"b" * 10)
        cached = store._accounting_fd
        assert cached is not None
        cached_fd = cached[1][0]
        child_pid = os.fork()
        if child_pid == 0:
            signal.alarm(5)
            try:
                os.fstat(cached_fd)
            except OSError:
                pass
            else:
                os._exit(2)
            replacement = os.open(os.devnull, os.O_RDONLY)
            if replacement != cached_fd:
                os.dup2(replacement, cached_fd)
                os.close(replacement)
            if not store.put(b"c" * 10):
                os._exit(3)
            if not artifacts_module.stat.S_ISCHR(os.fstat(cached_fd).st_mode):
                os._exit(4)
            store.close()
            if not artifacts_module.stat.S_ISCHR(os.fstat(cached_fd).st_mode):
                os._exit(5)
            os._exit(0)
        _, status = os.waitpid(child_pid, 0)

        assert os.waitstatus_to_exitcode(status) == 0

    @pytest.mark.serial
    @pytest.mark.timeout(0)
    @pytest.mark.filterwarnings("ignore:This process .* is multi-threaded.*:DeprecationWarning")
    def test_fork_child_close_resets_inherited_locked_mutex(self, tmp_path):
        if not hasattr(os, "fork") or not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("fork and descriptor-relative artifact I/O are required")
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=100)
        assert store.put(b"a" * 10)
        assert store.put(b"b" * 10)
        locked = threading.Event()
        release = threading.Event()

        def hold_store_lock() -> None:
            with store._lock:
                locked.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold_store_lock)
        holder.start()
        assert locked.wait(timeout=5)
        child_pid = os.fork()
        if child_pid == 0:
            signal.alarm(5)
            store.close()
            os._exit(0)
        try:
            _, status = os.waitpid(child_pid, 0)
        finally:
            release.set()
            holder.join(timeout=5)

        assert not holder.is_alive()
        assert os.waitstatus_to_exitcode(status) == 0

    def test_fork_child_hook_does_not_close_fd_reused_by_earlier_hook(self, tmp_path):
        if not hasattr(os, "fork") or not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("fork and descriptor-relative artifact I/O are required")
        script = r"""
import os
import signal
import stat
import sys

owned = {}

def replace_before_easycat_cleanup():
    fd = owned["fd"]
    os.close(fd)
    replacement = os.open(os.devnull, os.O_RDONLY)
    if replacement != fd:
        os.dup2(replacement, fd)
        os.close(replacement)

os.register_at_fork(after_in_child=replace_before_easycat_cleanup)

from easycat.runtime.artifacts import FilesystemArtifactStore

store = FilesystemArtifactStore("sess", data_dir=sys.argv[1], max_bytes=100)
assert store.put(b"a" * 10)
assert store.put(b"b" * 10)
cached = store._accounting_fd
assert cached is not None
owned["fd"] = cached[1][0]
child = os.fork()
if child == 0:
    signal.alarm(5)
    if not stat.S_ISCHR(os.fstat(owned["fd"]).st_mode):
        os._exit(2)
    if not store.put(b"c" * 10):
        os._exit(3)
    if not stat.S_ISCHR(os.fstat(owned["fd"]).st_mode):
        os._exit(4)
    os._exit(0)
_, status = os.waitpid(child, 0)
store.close()
raise SystemExit(os.waitstatus_to_exitcode(status))
"""
        data_dir = tmp_path / "earlier-hook"
        completed = subprocess.run(
            [sys.executable, "-c", script, str(data_dir)],
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    filter(
                        None,
                        (
                            str(Path(__file__).parents[2] / "src"),
                            os.environ.get("PYTHONPATH"),
                        ),
                    )
                ),
            },
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

        assert completed.returncode == 0, (
            f"stdout: {completed.stdout!r}\nstderr: {completed.stderr!r}"
        )

    def test_same_thread_fork_unwind_does_not_close_fd_reused_by_earlier_hook(
        self,
        tmp_path,
    ):
        if not hasattr(os, "fork") or not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("fork and descriptor-relative artifact I/O are required")
        script = r"""
import os
import signal
import stat
import sys

owned = {}

def replace_before_easycat_cleanup():
    fd = owned["fd"]
    os.close(fd)
    replacement = os.open(os.devnull, os.O_RDONLY)
    if replacement != fd:
        os.dup2(replacement, fd)
        os.close(replacement)

os.register_at_fork(after_in_child=replace_before_easycat_cleanup)

from easycat.runtime.artifacts import FilesystemArtifactStore

store = FilesystemArtifactStore("sess", data_dir=sys.argv[1], max_bytes=100)
child_mode = False
with store._reuse_session_fd_locked(create=True):
    owned["fd"] = next(iter(store._active_session_fds.values()))[0]
    child = os.fork()
    if child == 0:
        signal.alarm(5)
        child_mode = True
    else:
        _, status = os.waitpid(child, 0)
        if os.waitstatus_to_exitcode(status) != 0:
            raise SystemExit(os.waitstatus_to_exitcode(status))
if child_mode:
    if not stat.S_ISCHR(os.fstat(owned["fd"]).st_mode):
        os._exit(2)
    os._exit(0)
"""
        data_dir = tmp_path / "same-thread-unwind"
        completed = subprocess.run(
            [sys.executable, "-c", script, str(data_dir)],
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    filter(
                        None,
                        (
                            str(Path(__file__).parents[2] / "src"),
                            os.environ.get("PYTHONPATH"),
                        ),
                    )
                ),
            },
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

        assert completed.returncode == 0, (
            f"stdout: {completed.stdout!r}\nstderr: {completed.stderr!r}"
        )

    @pytest.mark.serial
    @pytest.mark.timeout(0)
    @pytest.mark.filterwarnings("ignore:This process .* is multi-threaded.*:DeprecationWarning")
    def test_fork_child_closes_session_fd_owned_by_another_thread(
        self,
        tmp_path,
        monkeypatch,
    ):
        if not hasattr(os, "fork") or not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("fork and descriptor-relative artifact I/O are required")
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=100)
        assert store.put(b"a" * 10)
        entered = threading.Event()
        release = threading.Event()
        active_fd: list[int] = []
        original_load = store._load_accounting_locked

        def pause_with_active_session_fd(*, persist_missing=True):
            active_fd.append(next(iter(store._active_session_fds.values()))[0])
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("timed out releasing active session fd")
            return original_load(persist_missing=persist_missing)

        monkeypatch.setattr(
            store,
            "_load_accounting_locked",
            pause_with_active_session_fd,
        )
        writer = threading.Thread(target=store.put, args=(b"b" * 10,))
        writer.start()
        assert entered.wait(timeout=5)
        child_pid = os.fork()
        if child_pid == 0:
            signal.alarm(5)
            try:
                os.fstat(active_fd[0])
            except OSError:
                os._exit(0)
            os._exit(2)
        try:
            _, status = os.waitpid(child_pid, 0)
        finally:
            release.set()
            writer.join(timeout=5)

        assert not writer.is_alive()
        assert os.waitstatus_to_exitcode(status) == 0

    def test_close_waits_for_concurrent_put_before_releasing_accounting_fd(
        self,
        tmp_path,
        monkeypatch,
    ):
        if not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=100)
        assert store.put(b"a" * 10)
        store.close()
        assert store._accounting_fd is None
        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        original_load = store._load_accounting_locked

        def pause_before_accounting_load(*, persist_missing=True):
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("timed out releasing accounting load")
            return original_load(persist_missing=persist_missing)

        monkeypatch.setattr(store, "_load_accounting_locked", pause_before_accounting_load)
        writer = threading.Thread(target=store.put, args=(b"b" * 10,))
        writer.start()
        assert entered.wait(timeout=5)
        closer = threading.Thread(target=lambda: (store.close(), closed.set()))
        closer.start()
        assert not closed.wait(timeout=0.1)

        release.set()
        writer.join(timeout=5)
        closer.join(timeout=5)

        assert not writer.is_alive()
        assert not closer.is_alive()
        assert closed.is_set()
        assert store._accounting_fd is None

    @pytest.mark.parametrize("use_fallback", [False, True])
    def test_hardlinked_accounting_refuses_rewrite(
        self,
        tmp_path,
        monkeypatch,
        use_fallback,
    ):
        if not use_fallback and not artifacts_module._SUPPORTS_DESCRIPTOR_ARTIFACT_IO:
            pytest.skip("descriptor-relative artifact I/O is unavailable")
        if use_fallback:
            monkeypatch.setattr(artifacts_module, "_SUPPORTS_DESCRIPTOR_ARTIFACT_IO", False)
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=100)
        assert store.put(b"a" * 10)
        accounting_path = store._accounting_path()
        external = tmp_path / "outside-accounting"
        try:
            os.link(accounting_path, external)
        except OSError:
            pytest.skip("hard links are unavailable in this test environment")
        original = external.read_bytes()
        payload = b"b" * 10

        assert store.put(payload) == ""

        assert external.read_bytes() == original
        assert not store.has(hashlib.sha256(payload).hexdigest())

    def test_accounting_write_failure_refuses_new_artifact(self, tmp_path, monkeypatch):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        payload = b"new"

        def fail_accounting_write(_accounting):
            raise OSError("accounting unavailable")

        monkeypatch.setattr(store, "_write_accounting_locked", fail_accounting_write)

        assert store.put(payload) == ""
        assert not store.has(hashlib.sha256(payload).hexdigest())

    def test_accounting_scan_runs_for_initial_seed_and_each_reopen(self, tmp_path, monkeypatch):
        scans = 0
        original = FilesystemArtifactStore._stored_bytes

        def count_scan(store):
            nonlocal scans
            scans += 1
            return original(store)

        monkeypatch.setattr(FilesystemArtifactStore, "_stored_bytes", count_scan)
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=100)

        assert store.put(b"a" * 10)
        assert store.put(b"b" * 10)
        reopened = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=100)

        assert reopened._current_bytes == 20
        assert scans == 2

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

    def test_restart_seeds_existing_byte_budget(self, tmp_path):
        first = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=100)
        ref = first.put(b"a" * 60)
        assert ref

        reopened = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=100)

        assert reopened._current_bytes == 60
        assert reopened.put(b"b" * 60) == ""
        assert reopened.get(ref) == b"a" * 60

    def test_legacy_flat_artifacts_remain_readable_and_counted(self, tmp_path):
        payload = b"legacy-data"
        ref = hashlib.sha256(payload).hexdigest()
        artifact_dir = tmp_path / "artifacts" / "sess"
        artifact_dir.mkdir(parents=True)
        legacy_path = artifact_dir / f"{ref}.bin"
        legacy_path.write_bytes(payload)

        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=len(payload))

        assert store._current_bytes == len(payload)
        assert store.get(ref) == payload
        assert store.has(ref)
        assert store.put(payload) == ref

    def test_delete_reclaims_seeded_byte_budget(self, tmp_path):
        store = FilesystemArtifactStore("sess", data_dir=tmp_path, max_bytes=10)
        ref = store.put(b"a" * 10)
        assert store._current_bytes == 10

        store.delete(ref)

        assert store._current_bytes == 0
        assert store.put(b"b" * 10)


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
