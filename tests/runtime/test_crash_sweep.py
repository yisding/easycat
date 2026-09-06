"""Tests for the orphaned-journal crash-durability sweep."""

from __future__ import annotations

import hashlib
import os
import select
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import pytest

from easycat.runtime import SqliteJournal, sweep_crashed_journals
from easycat.runtime import crash_sweep as crash_sweep_module
from easycat.runtime import journal_sql as journal_sql_module
from easycat.runtime.artifacts import FilesystemArtifactStore
from easycat.runtime.crash_sweep import (
    _boot_id,
    _copy_journal_to_crash_dump,
    _crashed_state,
    _process_birth_identity,
    _process_start_token,
    _process_start_wallclock,
    crash_dump_artifact_root,
    is_journal_live,
    self_birth_identity,
    snapshot_crash_dump_artifacts,
)
from easycat.runtime.records import JournalRecordKind


@pytest.fixture(autouse=True)
def _reset_sweep_coordination():
    journal_sql_module._clear_crash_sweep_states()
    yield
    journal_sql_module._clear_crash_sweep_states()


def _dead_pid() -> int:
    """Return a PID that has definitely exited (so it reads as not-alive)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _crash_one(
    session_id: str,
    tmp_path,
    *,
    name: str = "ev",
    input_ref: str | None = None,
) -> None:
    """Write a journal with rows and abandon it as a dead foreign process.

    Stamps the ``live_pid`` marker with an exited PID so the sweep's
    liveness check treats it like a genuinely-crashed process (the test
    runs in a live process whose own PID would otherwise read as alive).
    """
    j = SqliteJournal(session_id, data_dir=tmp_path)
    j.append(
        kind=JournalRecordKind.EVENT,
        name=name,
        session_id=session_id,
        input_ref=input_ref,
    )
    # Invalidate any scheduled batch while holding the journal lock, then
    # stamp a dead owner and drop the connection without a clean-close marker.
    with j._lock:
        j._commit_transaction_locked(reopen=False)
        j._conn.execute(
            "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid', ?)",
            (str(_dead_pid()),),
        )
        j._conn.commit()
        j._conn.close()
        j._closed = True
    j._release_live_journal()


def _crash_with_managed_artifacts(session_id: str, data_dir) -> tuple[str, str]:
    journal = SqliteJournal(session_id, data_dir=data_dir)
    store = FilesystemArtifactStore(session_id, data_dir=data_dir)
    committed_ref = store.put(b"committed crash artifact")
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="ev",
        session_id=session_id,
        input_ref=committed_ref,
    )
    orphan_ref = store.put(b"uncommitted crash artifact")
    with journal._lock:
        journal._commit_transaction_locked(reopen=False)
        journal._conn.execute(
            "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid', ?)",
            (str(_dead_pid()),),
        )
        journal._conn.commit()
        journal._conn.close()
        journal._closed = True
    journal._release_live_journal()
    store.close()
    return committed_ref, orphan_ref


def _crash_with_incomplete_artifact_snapshot(session_id: str, data_dir) -> tuple[str, str]:
    journal = SqliteJournal(session_id, data_dir=data_dir)
    store = FilesystemArtifactStore(session_id, data_dir=data_dir)
    missing_ref = store.put(b"missing before sweep")
    available_ref = store.put(b"must remain available")
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="ev",
        session_id=session_id,
        input_ref=missing_ref,
        output_ref=available_ref,
    )
    with journal._lock:
        journal._commit_transaction_locked(reopen=False)
        journal._conn.execute(
            "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid', ?)",
            (str(_dead_pid()),),
        )
        journal._conn.commit()
        journal._conn.close()
        journal._closed = True
    journal._release_live_journal()
    store._ref_path(missing_ref).unlink()
    store.close()
    return missing_ref, available_ref


def test_sqlite_construction_skips_repeat_sweep_within_interval(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(
        journal_sql_module,
        "sweep_crashed_journals",
        lambda root, *, skip: calls.append((root, skip)),
    )

    first = SqliteJournal("first", data_dir=tmp_path)
    second = SqliteJournal("second", data_dir=tmp_path)
    try:
        assert len(calls) == 1
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("directory_name", ["runs?blue", "runs#blue"])
def test_crash_sweep_handles_reserved_uri_characters(tmp_path, directory_name: str) -> None:
    data_dir = tmp_path / directory_name
    _crash_one("crashed", data_dir)

    assert sweep_crashed_journals(data_dir) == 1
    assert (data_dir / "crash-dumps" / "crashed.sqlite").exists()
    assert not (data_dir / "journals" / "crashed.sqlite").exists()


def test_crash_dump_snapshots_artifacts_away_from_reused_session(tmp_path) -> None:
    session_id = "reused"
    payload = b"prior caller audio"
    store = FilesystemArtifactStore(session_id, data_dir=tmp_path)
    ref = store.put(payload)
    _crash_one(session_id, tmp_path, input_ref=ref)

    assert sweep_crashed_journals(tmp_path) == 1

    crash_path = tmp_path / "crash-dumps" / f"{session_id}.sqlite"
    snapshot = crash_dump_artifact_root(crash_path)
    copied = snapshot / ref[:2] / f"{ref}.bin"
    assert copied.read_bytes() == payload

    # A future session is free to remove/recreate its own artifact root;
    # that cannot make this crash dump's post-mortem blobs disappear.
    shutil.rmtree(tmp_path / "artifacts" / session_id)
    assert copied.read_bytes() == payload


def test_different_session_sweep_retires_crashed_live_artifacts(tmp_path) -> None:
    committed_ref, orphan_ref = _crash_with_managed_artifacts("unique-old", tmp_path)
    journal_sql_module._clear_crash_sweep_states()

    fresh = SqliteJournal("unique-new", data_dir=tmp_path)
    fresh.close()

    crash_path = tmp_path / "crash-dumps" / "unique-old.sqlite"
    copied = crash_dump_artifact_root(crash_path) / committed_ref[:2] / f"{committed_ref}.bin"
    assert copied.read_bytes() == b"committed crash artifact"
    old_store = FilesystemArtifactStore("unique-old", data_dir=tmp_path)
    try:
        assert old_store.has(committed_ref) is False
        assert old_store.has(orphan_ref) is False
        assert old_store._current_bytes == 0
    finally:
        old_store.close()
    assert FilesystemArtifactStore._pending_journal_retirements(tmp_path) == ()


def test_sweep_retries_retirement_after_source_journal_is_gone(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    committed_ref, orphan_ref = _crash_with_managed_artifacts("retry-old", tmp_path)
    original_complete = FilesystemArtifactStore._complete_journal_retirement
    monkeypatch.setattr(
        FilesystemArtifactStore,
        "_complete_journal_retirement",
        lambda self: False,
    )

    assert sweep_crashed_journals(tmp_path) == 1
    assert not (tmp_path / "journals" / "retry-old.sqlite").exists()
    leaked = FilesystemArtifactStore("retry-old", data_dir=tmp_path)
    try:
        assert leaked.has(committed_ref)
        assert leaked.has(orphan_ref)
    finally:
        leaked.close()
    assert FilesystemArtifactStore._pending_journal_retirements(tmp_path) == ("retry-old",)

    monkeypatch.setattr(
        FilesystemArtifactStore,
        "_complete_journal_retirement",
        original_complete,
    )
    assert sweep_crashed_journals(tmp_path) == 0
    retired = FilesystemArtifactStore("retry-old", data_dir=tmp_path)
    try:
        assert retired.has(committed_ref) is False
        assert retired.has(orphan_ref) is False
        assert retired._current_bytes == 0
    finally:
        retired.close()
    assert FilesystemArtifactStore._pending_journal_retirements(tmp_path) == ()


def test_retirement_prepare_failure_keeps_source_for_retry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    committed_ref, orphan_ref = _crash_with_managed_artifacts("prepare-retry", tmp_path)
    original_write_epoch = FilesystemArtifactStore._write_artifact_epoch_locked

    def fail_epoch_write(self, epoch: str) -> None:
        raise OSError("injected epoch rotation failure")

    monkeypatch.setattr(
        FilesystemArtifactStore,
        "_write_artifact_epoch_locked",
        fail_epoch_write,
    )
    assert sweep_crashed_journals(tmp_path) == 0
    assert (tmp_path / "journals" / "prepare-retry.sqlite").exists()
    assert not (tmp_path / "crash-dumps" / "prepare-retry.sqlite").exists()
    assert FilesystemArtifactStore._pending_journal_retirements(tmp_path) == ("prepare-retry",)

    monkeypatch.setattr(
        FilesystemArtifactStore,
        "_write_artifact_epoch_locked",
        original_write_epoch,
    )
    assert sweep_crashed_journals(tmp_path) == 1
    assert not (tmp_path / "journals" / "prepare-retry.sqlite").exists()
    store = FilesystemArtifactStore("prepare-retry", data_dir=tmp_path)
    try:
        assert store.has(committed_ref) is False
        assert store.has(orphan_ref) is False
        assert store._current_bytes == 0
    finally:
        store.close()
    assert FilesystemArtifactStore._pending_journal_retirements(tmp_path) == ()


def test_same_id_reopen_completes_deferred_artifact_retirement(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    committed_ref, orphan_ref = _crash_with_managed_artifacts("retry-same", tmp_path)
    original_complete = FilesystemArtifactStore._complete_journal_retirement
    monkeypatch.setattr(
        FilesystemArtifactStore,
        "_complete_journal_retirement",
        lambda self: False,
    )
    assert sweep_crashed_journals(tmp_path) == 1
    monkeypatch.setattr(
        FilesystemArtifactStore,
        "_complete_journal_retirement",
        original_complete,
    )
    prestaged = FilesystemArtifactStore("retry-same", data_dir=tmp_path)
    replacement_ref = prestaged.put(b"replacement session artifact")
    prestaged.close()

    reopened = SqliteJournal("retry-same", data_dir=tmp_path)
    try:
        store = FilesystemArtifactStore("retry-same", data_dir=tmp_path)
        try:
            assert store.has(committed_ref) is False
            assert store.has(orphan_ref) is False
            assert store.get(replacement_ref) == b"replacement session artifact"
            assert store._current_bytes == len(b"replacement session artifact")
        finally:
            store.close()
        assert FilesystemArtifactStore._pending_journal_retirements(tmp_path) == ()
    finally:
        reopened.close()


def test_crash_dump_does_not_snapshot_artifacts_through_parent_symlink(tmp_path) -> None:
    session_id = "orphan"
    payload = b"outside artifact"
    outside_root = tmp_path / "outside"
    outside_store = FilesystemArtifactStore(session_id, data_dir=outside_root)
    ref = outside_store.put(payload)
    _crash_one(session_id, tmp_path, input_ref=ref)
    artifacts = tmp_path / "artifacts"
    try:
        artifacts.symlink_to(outside_root / "artifacts", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable in this test environment")

    assert sweep_crashed_journals(tmp_path) == 0

    crash_path = tmp_path / "crash-dumps" / f"{session_id}.sqlite"
    copied = crash_dump_artifact_root(crash_path) / ref[:2] / f"{ref}.bin"
    assert not copied.exists()
    assert (tmp_path / "journals" / f"{session_id}.sqlite").exists()
    assert outside_store.get(ref) == payload


def test_artifact_snapshot_rejects_symlinked_source_ancestor(tmp_path) -> None:
    session_id = "linked-artifacts"
    payload = b"outside payload"
    ref = hashlib.sha256(payload).hexdigest()
    _crash_one(session_id, tmp_path, input_ref=ref)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    # Journal startup may create live artifact-epoch metadata even when no
    # payload has been published. Replace that managed directory with the
    # unsafe ancestor this regression is intended to exercise.
    shutil.rmtree(artifacts / session_id)
    outside = tmp_path / "outside"
    shard = outside / ref[:2]
    shard.mkdir(parents=True)
    (shard / f"{ref}.bin").write_bytes(payload)
    try:
        (artifacts / session_id).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable in this test environment")
    target = tmp_path / "snapshot"
    target.mkdir()

    assert (
        snapshot_crash_dump_artifacts(
            tmp_path,
            tmp_path / "journals" / f"{session_id}.sqlite",
            target,
        )
        is False
    )

    assert list(target.rglob("*.bin")) == []


def test_incomplete_artifact_snapshot_keeps_source_and_remaining_live_blobs(tmp_path) -> None:
    session_id = "partial-snapshot"
    _, available_ref = _crash_with_incomplete_artifact_snapshot(session_id, tmp_path)

    assert sweep_crashed_journals(tmp_path) == 0
    assert (tmp_path / "journals" / f"{session_id}.sqlite").exists()
    assert not (tmp_path / "crash-dumps" / f"{session_id}.sqlite").exists()
    store = FilesystemArtifactStore(session_id, data_dir=tmp_path)
    assert store.get(available_ref) == b"must remain available"


def test_same_id_reopen_refuses_incomplete_artifact_snapshot(tmp_path) -> None:
    session_id = "partial-same-id"
    _, available_ref = _crash_with_incomplete_artifact_snapshot(session_id, tmp_path)

    with pytest.raises(RuntimeError, match="snapshot was incomplete"):
        SqliteJournal(session_id, data_dir=tmp_path)

    db_path = tmp_path / "journals" / f"{session_id}.sqlite"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM journal").fetchone() == (1,)
    finally:
        conn.close()
    store = FilesystemArtifactStore(session_id, data_dir=tmp_path)
    assert store.get(available_ref) == b"must remain available"
    assert not (tmp_path / "crash-dumps" / f"{session_id}.sqlite").exists()


def test_crash_copy_rejects_symlinked_wal_sidecar(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.sqlite"
    source.write_bytes(b"journal")
    victim = tmp_path / "victim"
    victim.write_bytes(b"private")
    sidecar = tmp_path / "source.sqlite-wal"
    try:
        sidecar.symlink_to(victim)
    except OSError:
        pytest.skip("symlinks are unavailable in this test environment")

    class _Connection:
        def execute(self, _query: str) -> None:
            raise sqlite3.OperationalError("skip checkpoint")

        def close(self) -> None:
            pass

    monkeypatch.setattr(crash_sweep_module.sqlite3, "connect", lambda *_a, **_k: _Connection())
    target = tmp_path / "target.sqlite"

    with pytest.raises(OSError):
        _copy_journal_to_crash_dump(source, target)

    assert victim.read_bytes() == b"private"
    assert not (tmp_path / "target.sqlite-wal").exists()


def test_repeated_crashes_for_reused_session_id_keep_each_dump(tmp_path) -> None:
    _crash_one("reused", tmp_path, name="first")
    assert sweep_crashed_journals(tmp_path) == 1

    _crash_one("reused", tmp_path, name="second")
    assert sweep_crashed_journals(tmp_path) == 1

    crash_dir = tmp_path / "crash-dumps"
    first_dump = crash_dir / "reused.sqlite"
    second_dump = crash_dir / "reused-1.sqlite"
    assert first_dump.exists()
    assert second_dump.exists()
    assert crash_dump_artifact_root(first_dump).is_dir()
    assert crash_dump_artifact_root(second_dump).is_dir()

    def event_name(path):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT name FROM journal WHERE sequence = 1").fetchone()[0]
        finally:
            conn.close()

    assert event_name(first_dump) == "first"
    assert event_name(second_dump) == "second"


def test_sqlite_construction_rescans_root_after_interval_and_finds_later_crash(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(journal_sql_module.time, "monotonic", lambda: now[0])

    first = SqliteJournal("first", data_dir=tmp_path)
    first.close()

    # This peer starts after the first scan, then dies while the current
    # process remains alive. The cached fast path initially leaves it alone.
    _crash_one("later-peer", tmp_path)
    before_due = SqliteJournal("before-due", data_dir=tmp_path)
    before_due.close()
    assert (tmp_path / "journals" / "later-peer.sqlite").exists()

    now[0] += journal_sql_module._CRASH_SWEEP_INTERVAL_SECONDS
    after_due = SqliteJournal("after-due", data_dir=tmp_path)
    try:
        assert (tmp_path / "crash-dumps" / "later-peer.sqlite").exists()
        assert not (tmp_path / "journals" / "later-peer.sqlite").exists()
    finally:
        after_due.close()


def test_crash_sweep_cache_is_bounded(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(journal_sql_module, "_CRASH_SWEEP_MAX_ROOTS", 2)
    monkeypatch.setattr(journal_sql_module, "sweep_crashed_journals", lambda root, *, skip: 0)

    roots = [tmp_path / f"root-{index}" for index in range(3)]
    for root in roots:
        journal_sql_module._sweep_crashed_journals_if_due(
            root,
            skip=root / "journals" / "own.sqlite",
        )

    assert list(journal_sql_module._CRASH_SWEEP_STATES) == [
        journal_sql_module._crash_sweep_key(roots[1]),
        journal_sql_module._crash_sweep_key(roots[2]),
    ]


def test_sweep_runs_eventually_without_a_third_opener(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(journal_sql_module, "_CRASH_SWEEP_INTERVAL_SECONDS", 0.02)
    first = SqliteJournal("first", data_dir=tmp_path)
    first.close()

    _crash_one("later-peer", tmp_path)
    crash_path = tmp_path / "crash-dumps" / "later-peer.sqlite"
    journal_path = tmp_path / "journals" / "later-peer.sqlite"
    deadline = time.monotonic() + 2.0
    while (not crash_path.exists() or journal_path.exists()) and time.monotonic() < deadline:
        time.sleep(0.01)

    assert crash_path.exists()
    assert not journal_path.exists()


def test_failed_sweep_retries_without_caching_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = threading.Event()
    calls = 0

    def sweep(_root, *, skip):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("transient")
        completed.set()
        return 0

    monkeypatch.setattr(journal_sql_module, "_CRASH_SWEEP_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(journal_sql_module, "sweep_crashed_journals", sweep)

    journal_sql_module._sweep_crashed_journals_if_due(
        tmp_path,
        skip=tmp_path / "journals" / "own.sqlite",
    )

    assert completed.wait(2.0)
    state = journal_sql_module._CRASH_SWEEP_STATES[journal_sql_module._crash_sweep_key(tmp_path)]
    assert calls == 2
    assert state.last_success is not None


def test_successful_sweep_timestamps_completion_not_start(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [10.0]

    def sweep(_root, *, skip):
        now[0] = 25.0
        return 0

    monkeypatch.setattr(journal_sql_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(journal_sql_module, "sweep_crashed_journals", sweep)

    journal_sql_module._sweep_crashed_journals_if_due(
        tmp_path,
        skip=tmp_path / "journals" / "own.sqlite",
    )

    state = journal_sql_module._CRASH_SWEEP_STATES[journal_sql_module._crash_sweep_key(tmp_path)]
    assert state.last_success == 25.0


def test_sweeps_for_distinct_roots_do_not_block_each_other(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slow_root = tmp_path / "slow"
    fast_root = tmp_path / "fast"
    slow_entered = threading.Event()
    release_slow = threading.Event()
    fast_completed = threading.Event()

    def sweep(root, *, skip):
        if root == slow_root.absolute():
            slow_entered.set()
            assert release_slow.wait(2.0)
        if root == fast_root.absolute():
            fast_completed.set()
        return 0

    monkeypatch.setattr(journal_sql_module, "sweep_crashed_journals", sweep)
    thread = threading.Thread(
        target=journal_sql_module._sweep_crashed_journals_if_due,
        args=(slow_root,),
        kwargs={"skip": slow_root / "journals" / "own.sqlite"},
    )
    thread.start()
    assert slow_entered.wait(2.0)
    try:
        journal_sql_module._sweep_crashed_journals_if_due(
            fast_root,
            skip=fast_root / "journals" / "own.sqlite",
        )
        assert fast_completed.is_set()
    finally:
        release_slow.set()
        thread.join(2.0)
    assert not thread.is_alive()


@pytest.mark.serial
@pytest.mark.timeout(0)
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
def test_sweep_coordination_lock_is_reset_after_fork(tmp_path) -> None:
    read_fd, write_fd = os.pipe()
    state = journal_sql_module._crash_sweep_state(tmp_path.absolute())
    state.lock.acquire()
    journal_sql_module._CRASH_SWEEP_STATE_LOCK.acquire()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            journal_sql_module._crash_sweep_state(tmp_path.absolute())
            os.write(write_fd, b"ok")
        finally:
            os._exit(0)

    os.close(write_fd)
    journal_sql_module._CRASH_SWEEP_STATE_LOCK.release()
    state.lock.release()
    try:
        ready, _, _ = select.select([read_fd], [], [], 2.0)
        assert ready and os.read(read_fd, 2) == b"ok"
    finally:
        os.close(read_fd)
        waited_pid, _ = os.waitpid(pid, os.WNOHANG)
        if waited_pid == 0:
            os.kill(pid, 9)
            os.waitpid(pid, 0)


def test_pid_reuse_does_not_keep_stale_owner_live(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = SqliteJournal("reused", data_dir=tmp_path)
    journal.append(kind=JournalRecordKind.EVENT, name="ev", session_id="reused")
    journal._conn.execute("COMMIT")
    journal._conn.execute(
        "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid', ?)",
        (str(os.getpid()),),
    )
    journal._conn.execute(
        "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid_start', ?)",
        ("prior-process",),
    )
    journal._conn.commit()
    journal._conn.close()
    journal._closed = True
    monkeypatch.setattr(
        "easycat.runtime.crash_sweep._process_birth_identity",
        lambda pid: "current-process",
    )

    db_path = tmp_path / "journals" / "reused.sqlite"
    assert _crashed_state(db_path) == "crashed"
    assert is_journal_live(db_path) is False


# ── Off-Linux process birth identity (gh 1067) ───────────────────


@pytest.fixture
def _no_proc_stat(monkeypatch: pytest.MonkeyPatch):
    """Simulate a host without ``/proc/<pid>/stat`` (macOS, the BSDs)."""
    monkeypatch.setattr("easycat.runtime.crash_sweep._process_start_token", lambda pid: None)
    monkeypatch.setattr("easycat.runtime.crash_sweep._SELF_BIRTH_IDENTITY", None)


def test_birth_identity_falls_back_to_wall_clock_start_without_proc(_no_proc_stat) -> None:
    """Off-Linux the identity comes from ``ps -o lstart=`` (gh 1067).

    ``_process_start_token`` reads ``/proc/<pid>/stat``, which does not exist
    on macOS, so ``_process_birth_identity`` returned ``None`` there — and a
    missing birth marker makes ``_has_live_pid`` read *any* live PID as the
    original owner.
    """
    identity = _process_birth_identity(os.getpid())

    assert identity is not None
    assert identity.startswith("lstart:")
    # The two forms are tagged, so a Linux marker can never be compared
    # against a wall-clock one as if they were the same scheme.
    assert ":" in identity


def test_wall_clock_start_is_none_for_a_dead_pid() -> None:
    assert _process_start_wallclock(_dead_pid()) is None


def test_self_birth_identity_is_computed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The off-Linux path spawns ``ps``, so the answer must be cached."""
    calls: list[int] = []

    def _counted(pid: int) -> str:
        calls.append(pid)
        return "lstart:Mon Jan  1 00:00:00 2035"

    monkeypatch.setattr("easycat.runtime.crash_sweep._SELF_BIRTH_IDENTITY", None)
    monkeypatch.setattr("easycat.runtime.crash_sweep._process_birth_identity", _counted)

    assert self_birth_identity() == "lstart:Mon Jan  1 00:00:00 2035"
    assert self_birth_identity() == "lstart:Mon Jan  1 00:00:00 2035"
    assert calls == [os.getpid()]


def test_recycled_pid_is_not_live_without_proc(tmp_path, _no_proc_stat) -> None:
    """A stale marker plus a recycled PID must not read as live off-Linux.

    Before the wall-clock fallback the marker was deleted rather than written
    on these hosts, so ``_has_live_pid`` saw "PID alive, no birth row" and
    answered True forever: reopening the session id raised
    ``Journal is active in process N``, and the sweep skipped the crashed file.
    """
    journal = SqliteJournal("recycled", data_dir=tmp_path)
    journal.append(kind=JournalRecordKind.EVENT, name="ev", session_id="recycled")
    birth_row = journal._conn.execute(
        "SELECT value FROM session_state WHERE key = 'live_pid_start'"
    ).fetchone()
    # The marker is written off-Linux now, instead of being deleted.
    assert birth_row is not None and str(birth_row[0]).startswith("lstart:")

    journal._conn.execute("COMMIT")
    # Simulate the crash: this PID number is now some *other* live process.
    journal._conn.execute(
        "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid_start', ?)",
        ("lstart:Mon Jan  1 00:00:00 2001",),
    )
    journal._conn.commit()
    journal._conn.close()
    journal._closed = True

    db_path = tmp_path / "journals" / "recycled.sqlite"
    assert is_journal_live(db_path) is False
    assert _crashed_state(db_path) == "crashed"


def test_recycled_pid_does_not_block_reopening_the_session_id(tmp_path, _no_proc_stat) -> None:
    """The claim gate must let the same session id be reopened (gh 1067)."""
    journal = SqliteJournal("reopen-me", data_dir=tmp_path)
    journal.append(kind=JournalRecordKind.EVENT, name="ev", session_id="reopen-me")
    journal._conn.execute("COMMIT")
    journal._conn.execute(
        "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid', ?)",
        # A PID that is alive but is not the journal's owner.
        (str(os.getppid()),),
    )
    journal._conn.execute(
        "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid_start', ?)",
        ("lstart:Mon Jan  1 00:00:00 2001",),
    )
    journal._conn.commit()
    journal._conn.close()
    journal._closed = True
    journal_sql_module._clear_crash_sweep_states()

    reopened = SqliteJournal("reopen-me", data_dir=tmp_path)
    reopened.close()


def test_sweep_promotes_crashed_orphan(tmp_path) -> None:
    _crash_one("orphan", tmp_path)
    journal = tmp_path / "journals" / "orphan.sqlite"
    assert journal.exists()

    promoted = sweep_crashed_journals(tmp_path)

    assert promoted == 1
    crash = tmp_path / "crash-dumps" / "orphan.sqlite"
    assert crash.exists()
    # The source is removed so it stops accumulating in journals/.
    assert not journal.exists()


def test_sweep_rechecks_liveness_after_acquiring_journal_claim(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session opening after classification must win before destructive sweep work."""
    _crash_one("victim", tmp_path)
    db_path = tmp_path / "journals" / "victim.sqlite"
    entered_claim = threading.Event()
    release_claim = threading.Event()
    original_claim = crash_sweep_module.journal_file_claim

    @contextmanager
    def delayed_claim(path, *, blocking):
        if path == db_path and not blocking:
            entered_claim.set()
            assert release_claim.wait(2)
        with original_claim(path, blocking=blocking) as claimed:
            yield claimed

    monkeypatch.setattr(crash_sweep_module, "journal_file_claim", delayed_claim)
    promoted: list[int] = []
    sweep_thread = threading.Thread(
        target=lambda: promoted.append(sweep_crashed_journals(tmp_path)),
    )
    sweep_thread.start()
    assert entered_claim.wait(2)

    live = SqliteJournal("victim", data_dir=tmp_path)
    try:
        release_claim.set()
        sweep_thread.join(2)
        assert not sweep_thread.is_alive()
        assert promoted == [0]
        assert db_path.exists()
        live.append(kind=JournalRecordKind.EVENT, name="live", session_id="victim")
    finally:
        release_claim.set()
        sweep_thread.join(2)
        live.close()


def test_failed_source_removal_does_not_accumulate_duplicate_dumps(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _crash_one("stuck", tmp_path)
    source = tmp_path / "journals" / "stuck.sqlite"
    original_remove = crash_sweep_module._remove_journal
    monkeypatch.setattr(crash_sweep_module, "_remove_journal", lambda _path: False)

    assert sweep_crashed_journals(tmp_path) == 0
    assert sweep_crashed_journals(tmp_path) == 0

    assert source.exists()
    assert list((tmp_path / "crash-dumps").iterdir()) == []
    assert FilesystemArtifactStore._pending_journal_retirements(tmp_path) == ("stuck",)

    monkeypatch.setattr(crash_sweep_module, "_remove_journal", original_remove)
    assert sweep_crashed_journals(tmp_path) == 1
    assert not source.exists()
    assert (tmp_path / "crash-dumps" / "stuck.sqlite").exists()
    assert FilesystemArtifactStore._pending_journal_retirements(tmp_path) == ()


def test_sweep_promotes_orphan_when_pid_was_reused(tmp_path) -> None:
    """An alive PID with the wrong start token is not the journal owner."""
    start_token = _process_start_token(os.getpid())
    boot_id = _boot_id()
    if start_token is None or boot_id is None:
        pytest.skip("process start identity requires readable Linux /proc")

    journal = SqliteJournal("reused", data_dir=tmp_path)
    journal.append(kind=JournalRecordKind.EVENT, name="ev", session_id="reused")
    journal._conn.execute("COMMIT")
    journal._conn.execute(
        "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid', ?)",
        (str(os.getpid()),),
    )
    journal._conn.execute(
        "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid_start', ?)",
        (f"{boot_id}:{int(start_token) + 1}",),
    )
    journal._conn.commit()
    journal._conn.close()
    journal._closed = True
    db_path = tmp_path / "journals" / "reused.sqlite"

    assert is_journal_live(db_path) is False
    assert sweep_crashed_journals(tmp_path) == 1
    assert not db_path.exists()
    assert (tmp_path / "crash-dumps" / "reused.sqlite").exists()


def test_live_owner_marker_includes_process_start_identity(tmp_path) -> None:
    start_token = _process_start_token(os.getpid())
    boot_id = _boot_id()
    if start_token is None or boot_id is None:
        pytest.skip("full process identity requires readable Linux /proc")

    journal = SqliteJournal("owned", data_dir=tmp_path)
    try:
        marker = journal._conn.execute(
            "SELECT value FROM session_state WHERE key = 'live_pid'"
        ).fetchone()
        birth_marker = journal._conn.execute(
            "SELECT value FROM session_state WHERE key = 'live_pid_start'"
        ).fetchone()
        assert marker is not None
        assert marker[0] == str(os.getpid())
        assert birth_marker is not None
        assert birth_marker[0] == f"{boot_id}:{start_token}"
        assert birth_marker[0] == _process_birth_identity(os.getpid())
        assert is_journal_live(tmp_path / "journals" / "owned.sqlite") is True
    finally:
        journal.close()


def test_sweep_promotes_orphan_when_boot_identity_changed(tmp_path) -> None:
    """A matching PID/start token from a different boot is not the owner."""
    start_token = _process_start_token(os.getpid())
    boot_id = _boot_id()
    if start_token is None or boot_id is None:
        pytest.skip("full process identity requires readable Linux /proc")

    journal = SqliteJournal("prior-boot", data_dir=tmp_path)
    journal.append(kind=JournalRecordKind.EVENT, name="ev", session_id="prior-boot")
    journal._conn.execute("COMMIT")
    journal._conn.execute(
        "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid', ?)",
        (str(os.getpid()),),
    )
    journal._conn.execute(
        "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid_start', ?)",
        (f"not-{boot_id}:{start_token}",),
    )
    journal._conn.commit()
    journal._conn.close()
    journal._closed = True
    db_path = tmp_path / "journals" / "prior-boot.sqlite"

    assert is_journal_live(db_path) is False
    assert sweep_crashed_journals(tmp_path) == 1
    assert not db_path.exists()
    assert (tmp_path / "crash-dumps" / "prior-boot.sqlite").exists()


def test_sweep_leaves_clean_closed_journal(tmp_path) -> None:
    j = SqliteJournal("clean", data_dir=tmp_path)
    j.append(kind=JournalRecordKind.EVENT, name="ev", session_id="clean")
    j.close()  # writes the clean_close marker

    promoted = sweep_crashed_journals(tmp_path)

    assert promoted == 0
    assert (tmp_path / "journals" / "clean.sqlite").exists()
    assert not (tmp_path / "crash-dumps" / "clean.sqlite").exists()


def test_sweep_skips_empty_journal(tmp_path) -> None:
    # A journal file with schema but no rows (and no clean_close) is not a
    # crash — nothing to recover, so leave it alone even after its owning
    # process is gone.
    j = SqliteJournal("empty", data_dir=tmp_path)
    j._conn.execute(
        "INSERT OR REPLACE INTO session_state (key, value) VALUES ('live_pid', ?)",
        (str(_dead_pid()),),
    )
    j._conn.commit()
    j._conn.close()
    j._closed = True

    assert _crashed_state(tmp_path / "journals" / "empty.sqlite") == "empty"

    promoted = sweep_crashed_journals(tmp_path)
    assert promoted == 0
    assert not (tmp_path / "crash-dumps" / "empty.sqlite").exists()


def test_sweep_skips_locked_live_journal_without_raising(tmp_path) -> None:
    # An open live session holds a write transaction on its journal; the
    # sweep must classify it as live (skip), not promote it, and not raise.
    # A separate session id forces the orphan-sweep path (not same-id
    # recovery) so we exercise the liveness probe directly.
    live = SqliteJournal("live", data_dir=tmp_path)
    live.append(kind=JournalRecordKind.EVENT, name="ev", session_id="live")
    try:
        promoted = sweep_crashed_journals(tmp_path)
    finally:
        live.close()

    assert promoted == 0
    # The live journal stays put; it was never promoted.
    assert (tmp_path / "journals" / "live.sqlite").exists()
    assert not (tmp_path / "crash-dumps" / "live.sqlite").exists()


def test_sweep_skips_malformed_sqlite_without_raising(tmp_path) -> None:
    journals = tmp_path / "journals"
    journals.mkdir()
    bad = journals / "bad.sqlite"
    bad.write_text("not a sqlite database")

    assert _crashed_state(bad) == "skip"
    assert sweep_crashed_journals(tmp_path) == 0
    assert bad.exists()


def test_sweep_ignores_symlinked_journal_file(tmp_path) -> None:
    outside_root = tmp_path / "outside"
    _crash_one("target", outside_root)
    target = outside_root / "journals" / "target.sqlite"
    journals = tmp_path / "journals"
    journals.mkdir()
    linked = journals / "linked.sqlite"
    linked.symlink_to(target)

    assert sweep_crashed_journals(tmp_path) == 0
    assert linked.is_symlink()
    assert target.exists()
    assert not (tmp_path / "crash-dumps").exists()


def test_sweep_refuses_symlinked_crash_dump_directory(tmp_path) -> None:
    _crash_one("orphan", tmp_path)
    source = tmp_path / "journals" / "orphan.sqlite"
    target = tmp_path / "outside-dumps"
    target.mkdir()
    os.chmod(target, 0o755)
    crash_dumps = tmp_path / "crash-dumps"
    crash_dumps.symlink_to(target, target_is_directory=True)

    assert sweep_crashed_journals(tmp_path) == 0
    assert source.exists()
    assert crash_dumps.is_symlink()
    assert target.stat().st_mode & 0o777 == 0o755
    assert list(target.iterdir()) == []


def test_sweep_fails_if_reserved_artifact_root_is_replaced_by_symlink(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _crash_one("orphan", tmp_path)
    source = tmp_path / "journals" / "orphan.sqlite"
    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    marker = outside / "keep.bin"
    marker.write_bytes(b"external")
    original_reserve = crash_sweep_module.reserve_crash_dump_paths

    def replace_reservation(root, session_id):
        crash_path, artifact_root = original_reserve(root, session_id)
        artifact_root.rmdir()
        try:
            artifact_root.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are unavailable in this test environment")
        return crash_path, artifact_root

    monkeypatch.setattr(
        crash_sweep_module,
        "reserve_crash_dump_paths",
        replace_reservation,
    )

    assert sweep_crashed_journals(tmp_path) == 0
    assert source.exists()
    assert not (tmp_path / "crash-dumps" / "orphan.sqlite").exists()
    assert not (tmp_path / "crash-dumps" / "orphan.artifacts").is_symlink()
    assert marker.read_bytes() == b"external"


def test_sqlite_journal_open_ignores_malformed_sibling_journal(tmp_path) -> None:
    journals = tmp_path / "journals"
    journals.mkdir()
    bad = journals / "bad.sqlite"
    bad.write_text("not a sqlite database")

    journal = SqliteJournal("fresh", data_dir=tmp_path)
    try:
        assert (journals / "fresh.sqlite").exists()
        assert bad.exists()
    finally:
        journal.close()


def test_sweep_skips_the_caller_owned_path(tmp_path) -> None:
    _crash_one("mine", tmp_path)
    own = tmp_path / "journals" / "mine.sqlite"

    promoted = sweep_crashed_journals(tmp_path, skip=own)

    assert promoted == 0
    assert own.exists()
    assert not (tmp_path / "crash-dumps" / "mine.sqlite").exists()


def test_sweep_runs_on_next_sqlite_open(tmp_path) -> None:
    # A *different* session id whose process crashed is promoted the next
    # time any SqliteJournal opens — the same-id recovery path never fires
    # for an orphaned id.
    _crash_one("ghost", tmp_path)
    assert (tmp_path / "journals" / "ghost.sqlite").exists()
    # Simulate the fresh process that follows the stamped dead owner.
    journal_sql_module._clear_crash_sweep_states()

    j2 = SqliteJournal("fresh", data_dir=tmp_path)
    try:
        crash = tmp_path / "crash-dumps" / "ghost.sqlite"
        assert crash.exists()
        assert not (tmp_path / "journals" / "ghost.sqlite").exists()
    finally:
        j2.close()


def test_sweep_does_not_promote_own_journal_on_open(tmp_path) -> None:
    # Re-opening the same crashed id must go through the same-id recovery
    # path (which writes a recovery marker), not the orphan sweep.
    _crash_one("sess", tmp_path)

    j2 = SqliteJournal("sess", data_dir=tmp_path)
    try:
        # Same-id recovery promotes via _reconcile_prior_session, marking
        # _recovered True; the sweep must have left this path alone.
        assert j2._recovered is True
    finally:
        j2.close()


def test_sweep_missing_journals_dir_is_noop(tmp_path) -> None:
    assert sweep_crashed_journals(tmp_path) == 0


def test_promoted_crash_dump_preserves_records(tmp_path) -> None:
    _crash_one("payload", tmp_path)
    sweep_crashed_journals(tmp_path)

    crash = tmp_path / "crash-dumps" / "payload.sqlite"
    conn = sqlite3.connect(f"file:{crash}?mode=ro", uri=True)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM journal ORDER BY sequence")]
    finally:
        conn.close()
    assert "ev" in names


def test_crashed_state_classification(tmp_path) -> None:
    # crashed: rows, no clean_close
    _crash_one("c", tmp_path)
    assert _crashed_state(tmp_path / "journals" / "c.sqlite") == "crashed"

    # clean: clean_close marker present
    j = SqliteJournal("ok", data_dir=tmp_path)
    j.append(kind=JournalRecordKind.EVENT, name="ev", session_id="ok")
    j.close()
    assert _crashed_state(tmp_path / "journals" / "ok.sqlite") == "clean"

    # skip: file without the journal schema
    bare = tmp_path / "journals" / "bare.sqlite"
    conn = sqlite3.connect(bare)
    conn.execute("CREATE TABLE other (x INTEGER)")
    conn.commit()
    conn.close()
    assert _crashed_state(bare) == "skip"


def test_copy_journal_to_crash_dump_copies_sidecars(tmp_path) -> None:
    _crash_one("side", tmp_path)
    src = tmp_path / "journals" / "side.sqlite"
    dst = tmp_path / "crash-dumps" / "side.sqlite"
    dst.parent.mkdir(parents=True, exist_ok=True)

    _copy_journal_to_crash_dump(src, dst)

    assert dst.exists()
    conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    try:
        count = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    finally:
        conn.close()
    assert count >= 1


@pytest.mark.parametrize("bad", ["", "0", None])
def test_crashed_state_treats_falsey_clean_close_as_crashed(tmp_path, bad) -> None:
    # A clean_close value that is empty/"0"/NULL is not a clean close.
    _crash_one("falsey", tmp_path)
    db = tmp_path / "journals" / "falsey.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO session_state (key, value) VALUES ('clean_close', ?)",
            (bad,),
        )
        conn.commit()
    finally:
        conn.close()
    assert _crashed_state(db) == "crashed"
