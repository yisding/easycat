"""Crash-durability sweep for orphaned journals under ``journals/``.

A session whose process died before a clean close leaves its SQLite file in
``journals/`` with no ``clean_close`` marker. The owning session is gone, so
the in-session crash-recovery path (which only fires when the *same*
``session_id`` is reopened — see ``SqliteJournal._reconcile_prior_session``)
will never promote it.  :func:`sweep_crashed_journals` closes that gap: it
runs once at every ``SqliteJournal`` open and scans ``journals/`` for
crashed-but-unswept files, promoting each to ``crash-dumps/`` so it surfaces
in ``easycat bundles list`` and stops accumulating in the live directory.

The sweep is strictly best-effort: it skips locked (live) databases and any
file it cannot read, and never raises into journal startup.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Final

from easycat.runtime._journal_lock import journal_file_claim
from easycat.runtime._private_files import (
    copy_private_file,
    mkdir_private,
    sqlite_readonly_uri,
)
from easycat.runtime.artifacts import FilesystemArtifactStore

logger = logging.getLogger(__name__)

_ARTIFACT_REF = re.compile(r"[0-9a-f]{64}")


def _boot_id() -> str | None:
    """Return Linux's stable identifier for the current boot, when available."""
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return boot_id if boot_id and ":" not in boot_id else None


def _process_start_token(pid: int) -> str | None:
    """Return Linux's stable start-time token for *pid*, when available.

    ``/proc/<pid>/stat`` field 22 is the process start time in clock ticks
    since boot. Pairing it with the PID distinguishes the original journal
    owner from a later process that inherited the same numeric PID.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, UnicodeError):
        return None
    # Field 2 (``comm``) is parenthesized and may itself contain spaces or
    # parentheses. Split after its final ")" so the remainder starts at
    # field 3; field 22 is therefore index 19.
    closing_paren = stat.rfind(")")
    if closing_paren < 0:
        return None
    fields = stat[closing_paren + 1 :].split()
    if len(fields) <= 19:
        return None
    token = fields[19]
    return token if token.isdecimal() else None


# ``ps`` should answer instantly; the bound exists so a wedged process table
# cannot stall a journal open.
_PS_TIMEOUT_S: Final = 2.0


def _process_start_wallclock(pid: int) -> str | None:
    """Return ``ps -o lstart=`` for *pid* on hosts without ``/proc/<pid>/stat``.

    macOS and the BSDs expose no ``/proc``, so the Linux start-ticks token is
    unavailable there.  ``ps -o lstart=`` reports an *absolute* wall-clock
    start time, which needs no boot scoping to be unambiguous (unlike the
    boot-relative ticks, which repeat across reboots).

    Its one-second granularity is coarse, but a PID recycled inside the same
    second as the crash it is compared against is not a case worth engineering
    for — and the alternative is what this replaces: no identity at all, which
    reads every recycled PID as the original owner.

    Best-effort throughout: any failure returns ``None``, and the caller then
    falls back to the conservative "assume live" answer that was the only
    behaviour available off-Linux before.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    # Collapse whitespace: ``lstart`` pads single-digit days ("Sep  5").
    value = " ".join(result.stdout.split())
    return value or None


def _process_birth_identity(pid: int) -> str | None:
    """Return a process-birth identity that a recycled PID cannot forge.

    Pairing the PID with the owning process's birth is what lets
    :func:`_has_live_pid` tell "the original owner is still running" from "the
    OS handed that number to somebody else".  Without one, a stale ``live_pid``
    left by a crash permanently blocks reopening the same session id once the
    number is recycled, and the crash sweep classifies the crashed journal as
    live and never promotes it (gh 1067).

    Linux reads ``/proc/<pid>/stat``'s boot-relative start ticks and scopes
    them with the boot id.  Everywhere else falls back to ``ps -o lstart=``,
    an absolute wall-clock start time that is already unambiguous.  The two
    forms are tagged so they can never be compared across a platform change.
    """
    start_token = _process_start_token(pid)
    if start_token is not None:
        boot_id = _boot_id()
        if boot_id is None:
            return None
        return f"{boot_id}:{start_token}"
    wallclock = _process_start_wallclock(pid)
    if wallclock is None:
        return None
    return f"lstart:{wallclock}"


_SELF_BIRTH_IDENTITY: tuple[int, str | None] | None = None


def self_birth_identity() -> str | None:
    """Cached :func:`_process_birth_identity` for the current process.

    A process's own birth never changes, and the off-Linux path spawns ``ps``,
    so the answer is computed once instead of on every journal open.  The
    cache is keyed by PID so a forked child recomputes its own.
    """
    global _SELF_BIRTH_IDENTITY
    pid = os.getpid()
    cached = _SELF_BIRTH_IDENTITY
    if cached is not None and cached[0] == pid:
        return cached[1]
    identity = _process_birth_identity(pid)
    _SELF_BIRTH_IDENTITY = (pid, identity)
    return identity


def _pid_alive(pid: int) -> bool:
    """Best-effort check whether *pid* names a running process.

    ``os.kill(pid, 0)`` raises ``ProcessLookupError`` if no such process
    exists and ``PermissionError`` if it exists but we may not signal it
    (still alive).  Treat any other error as "assume alive" so we never
    promote a journal a live process might still own.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _copy_journal_to_crash_dump(db_path: Path, crash_path: Path) -> None:
    """Checkpoint *db_path* and copy it (plus WAL/SHM sidecars) to *crash_path*.

    The caller must ensure no connection it owns is holding *db_path* open
    across this call (the in-session promoter closes its live connection
    first; the sweep operates on orphaned files no one owns).  We open our
    own short-lived connection to fold any uncheckpointed WAL pages into the
    main database before the byte copy — even with bounded auto-checkpointing,
    recent committed records may live only in the WAL and a bare copy would
    lose them.
    """
    if db_path.is_symlink():
        raise OSError(f"Refusing symlinked journal path: {db_path}")
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.OperationalError:
        pass  # Best-effort; copy WAL sidecars below as a fallback.

    copy_private_file(db_path, crash_path)
    # Also copy WAL/SHM sidecars if the checkpoint was incomplete (e.g. a
    # concurrent reader held the file) so no committed page is lost.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        crash_sidecar = Path(str(crash_path) + suffix)
        try:
            copy_private_file(sidecar, crash_sidecar)
        except FileNotFoundError:
            pass


def crash_dump_artifact_root(crash_path: Path) -> Path:
    """Return the dump-owned artifact snapshot path for *crash_path*."""
    return crash_path.with_name(f"{crash_path.stem}.artifacts")


def reserve_crash_dump_paths(root: Path, session_id: str) -> tuple[Path, Path]:
    """Reserve collision-free paths for one crash dump and its artifacts.

    Keep the historic ``<session_id>.sqlite`` name for the first dump.  A
    repeated crash for a reused session id gets a numeric suffix rather than
    overwriting the previous post-mortem.  Creating the artifact directory
    acts as an exclusive reservation, so another promoter cannot pick the
    same name between its existence check and copy.
    """
    crash_dir = root / "crash-dumps"
    mkdir_private(crash_dir)
    suffix = 0
    while True:
        suffix_text = "" if suffix == 0 else f"-{suffix}"
        crash_path = crash_dir / f"{session_id}{suffix_text}.sqlite"
        artifact_root = crash_dump_artifact_root(crash_path)
        if crash_path.exists():
            suffix += 1
            continue
        try:
            artifact_root.mkdir(mode=0o700)
        except FileExistsError:
            suffix += 1
            continue
        os.chmod(artifact_root, 0o700)
        return crash_path, artifact_root


def snapshot_crash_dump_artifacts(
    root: Path,
    db_path: Path,
    artifact_root: Path,
) -> bool:
    """Copy the artifacts referenced by *db_path* into a reserved snapshot.

    The snapshot is deliberately all-or-nothing.  If a referenced blob is
    missing or the journal cannot be read, leave the reserved directory empty
    and return ``False`` so callers retain the source journal and live store.
    """
    try:
        unsafe_target = artifact_root.is_symlink() or not artifact_root.is_dir()
    except OSError as exc:
        raise OSError(f"Crash artifact reservation is unavailable: {artifact_root}") from exc
    if unsafe_target:
        raise OSError(f"Crash artifact reservation is unsafe: {artifact_root}")

    refs = _referenced_artifact_refs(db_path)
    if refs is None:
        return False
    if not refs:
        return True

    artifacts_dir = root / "artifacts"
    source_root = artifacts_dir / db_path.stem
    try:
        unsafe_source = (
            artifacts_dir.is_symlink() or source_root.is_symlink() or not source_root.is_dir()
        )
    except OSError:
        unsafe_source = True
    if unsafe_source:
        return False

    sources: list[tuple[str, Path]] = []
    for ref in refs:
        source = _artifact_source_path(source_root, ref)
        if source is None:
            logger.warning(
                "Crash journal %s references unavailable artifacts; "
                "keeping it without an artifact snapshot",
                db_path,
            )
            return False
        sources.append((ref, source))

    for ref, source in sources:
        target_dir = artifact_root / ref[:2]
        mkdir_private(target_dir)
        target = target_dir / f"{ref}.bin"
        copy_private_file(source, target)
    return True


def discard_crash_dump(crash_path: Path, artifact_root: Path) -> None:
    """Best-effort cleanup for a failed dump copy and its reservation."""
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(crash_path) + suffix).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("Failed to remove incomplete crash dump %s", crash_path, exc_info=True)
    try:
        if artifact_root.is_symlink() or not artifact_root.is_dir():
            artifact_root.unlink(missing_ok=True)
        else:
            shutil.rmtree(str(artifact_root))
    except OSError:
        # The reservation can be swapped for a symlink after the first check.
        # A second lstat-style check lets cleanup remove only that link, never
        # the directory it targets.
        try:
            if artifact_root.is_symlink():
                artifact_root.unlink()
                return
        except OSError:
            pass
        logger.debug(
            "Failed to remove incomplete crash artifact reservation %s",
            artifact_root,
            exc_info=True,
        )


def _referenced_artifact_refs(db_path: Path) -> set[str] | None:
    """Read validated artifact refs from a journal without mutating it."""
    try:
        conn = sqlite3.connect(sqlite_readonly_uri(db_path), uri=True)
    except sqlite3.DatabaseError:
        return None
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(journal)")}
        if not {"input_ref", "output_ref"} & columns:
            return set()
        select_columns = [column for column in ("input_ref", "output_ref") if column in columns]
        refs: set[str] = set()
        query = f"SELECT {', '.join(select_columns)} FROM journal"
        for row in conn.execute(query):
            for ref in row:
                if isinstance(ref, str) and _ARTIFACT_REF.fullmatch(ref):
                    refs.add(ref)
        return refs
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()


def _artifact_source_path(source_root: Path, ref: str) -> Path | None:
    """Find one non-symlink artifact in sharded or legacy-flat storage."""
    for path in (source_root / ref[:2] / f"{ref}.bin", source_root / f"{ref}.bin"):
        try:
            valid_source = (
                not path.parent.is_symlink() and path.is_file() and not path.is_symlink()
            )
        except OSError:
            continue
        if valid_source:
            return path
    return None


def _crashed_state(db_path: Path) -> str:
    """Classify *db_path*: ``"crashed"``, ``"clean"``, ``"empty"``, or ``"skip"``.

    ``"skip"`` covers **live** databases (held open by a running session),
    locked/unreadable files, and missing journal schema — none of which we
    may promote.

    Classification is done **read-only first** so a cleanly-closed or
    foreign journal is never opened for writing (a write connection would
    checkpoint and clobber its WAL sidecar).  Only a file that reads as
    ``"crashed"`` gets a final ``BEGIN IMMEDIATE`` write-lock probe to rule
    out an actively-writing session whose ``live_pid`` marker might be stale
    (PID reuse) — and that file is about to be copied+deleted anyway.

    Liveness is decided two ways, both required because an idle WAL journal
    between turns holds **no** write lock and a read-only reader does not
    block on a writer:

    1. A ``live_pid`` owner marker (written on journal open, cleared on
       clean close). On Linux it pairs the PID with the process start token
       and boot ID, so PID reuse within or across boots does not create a
       false owner. A matching owner catches the idle-but-live window a lock
       probe would miss.
    2. A ``BEGIN IMMEDIATE`` write-lock probe on a would-be crash, as a
       backstop for an actively-writing session: if the lock is held, skip.
    """
    try:
        if db_path.is_symlink():
            return "skip"
    except OSError:
        return "skip"
    read_state = _read_only_state(db_path)
    if read_state != "crashed":
        return read_state
    # Looks crashed on a read; confirm no live writer holds the lock before
    # we promote (and delete) it.
    try:
        conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=0)
    except sqlite3.OperationalError:
        return "skip"
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return "skip"  # A live session is writing.
        conn.execute("ROLLBACK")
        return "crashed"
    except sqlite3.OperationalError:
        return "skip"
    finally:
        conn.close()


def _read_only_state(db_path: Path) -> str:
    """Read-only classification of *db_path* (never opens for writing)."""
    try:
        conn = sqlite3.connect(sqlite_readonly_uri(db_path), uri=True)
    except sqlite3.DatabaseError:
        return "skip"
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "journal" not in tables or "session_state" not in tables:
            return "skip"
        if _has_live_pid(conn):
            return "skip"
        clean = conn.execute(
            "SELECT value FROM session_state WHERE key = 'clean_close'"
        ).fetchone()
        if clean is not None and clean[0] not in (None, "", "0"):
            return "clean"
        count_row = conn.execute("SELECT COUNT(*) FROM journal").fetchone()
        if not count_row or count_row[0] == 0:
            return "empty"
        return "crashed"
    except sqlite3.DatabaseError:
        return "skip"
    finally:
        conn.close()


def _has_live_pid(conn: sqlite3.Connection) -> bool:
    """True if the journal's PID and process-birth markers identify a live owner."""
    row = conn.execute("SELECT value FROM session_state WHERE key = 'live_pid'").fetchone()
    if row is None or row[0] in (None, ""):
        return False
    try:
        pid = int(row[0])
    except (TypeError, ValueError):
        return False
    if not _pid_alive(pid):
        return False

    birth_row = conn.execute(
        "SELECT value FROM session_state WHERE key = 'live_pid_start'"
    ).fetchone()
    if birth_row is None or birth_row[0] in (None, ""):
        return True
    current_birth = _process_birth_identity(pid)
    if current_birth is None:
        return True
    return str(birth_row[0]) == current_birth


def is_journal_live(db_path: Path) -> bool:
    """True if *db_path* is currently owned by a running session.

    A "live" journal must never be archived, checkpointed, or removed by a
    retention sweep (doing so on a shared ``journals/`` directory — e.g.
    telephony with many concurrent sessions — would corrupt or lose an
    in-flight recording).  Liveness mirrors the crash-sweep decision and is
    decided two complementary ways, both required because an idle WAL journal
    between turns holds **no** write lock yet is still owned:

    1. A ``live_pid`` owner marker (written on journal open, cleared on
       clean close). On Linux the PID and process start token must both
       match. This catches idle live journals without confusing them with
       crashed journals whose numeric PID was later reused.
    2. A ``BEGIN IMMEDIATE`` write-lock probe as a backstop for an
       actively-writing session whose ``live_pid`` marker might be stale
       (PID reuse): if the lock cannot be taken, treat the journal as live.

    Any file we cannot open or read read-only is treated as live (returns
    ``True``) so retention errs on the side of preservation rather than
    deleting a database we simply failed to classify.

    Classification is **read-only first** (it never opens a cleanly-closed or
    foreign journal for writing, which would checkpoint and rewrite its WAL
    sidecar). Only a journal that lacks both a clean-close marker and a
    matching owner identity gets the final ``BEGIN IMMEDIATE`` write-lock
    probe — the same gate the crash sweep uses — so a kept-but-idle valid
    journal is never mutated.
    """
    try:
        if db_path.is_symlink():
            return True
    except OSError:
        return True
    try:
        conn = sqlite3.connect(sqlite_readonly_uri(db_path), uri=True)
    except sqlite3.DatabaseError:
        return True  # Unreadable -> preserve rather than risk a live DB.
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "session_state" not in tables:
            return False  # No journal schema -> not one of our live sessions.
        if _has_live_pid(conn):
            return True  # Marker names a running process -> live.
        clean = conn.execute(
            "SELECT value FROM session_state WHERE key = 'clean_close'"
        ).fetchone()
        if clean is not None and clean[0] not in (None, "", "0"):
            return False  # Cleanly closed -> definitively not live.
    except sqlite3.DatabaseError:
        return True
    finally:
        conn.close()

    # No matching owner marker and no clean-close marker: an actively-writing
    # session may hold the lock with a stale/absent marker. Backstop with a
    # write-lock probe (only reached for would-be-crashed files, so the WAL
    # rewrite a write connection causes is harmless).
    try:
        probe = sqlite3.connect(str(db_path), isolation_level=None, timeout=0)
    except sqlite3.OperationalError:
        return True
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("ROLLBACK")
        return False  # Lock acquired -> no live writer.
    except sqlite3.OperationalError:
        return True  # Lock held by a live writer.
    finally:
        probe.close()


def sweep_crashed_journals(data_dir: str | Path, *, skip: Path | None = None) -> int:
    """Promote crashed-but-unswept journals from ``journals/`` to ``crash-dumps/``.

    Scans ``<data_dir>/journals/*.sqlite``.  For each file that is **not**
    locked by a live session, lacks a ``clean_close`` marker, and has at
    least one journal row, copy it to ``<data_dir>/crash-dumps/<stem>.sqlite``
    (checkpointing WAL first) and remove the source.  Returns the number of
    files promoted.

    *skip* names a path the caller is about to open itself (the live
    session's own journal) and must never be promoted.  Cleanly-closed,
    empty, locked, or unreadable databases are left untouched.  Best-effort:
    individual failures are logged and skipped, never raised.
    """
    root = Path(data_dir)
    _retry_artifact_retirements(root, skip=skip)
    journals_dir = root / "journals"
    if not journals_dir.is_dir():
        return 0

    skip_resolved = None
    if skip is not None:
        try:
            skip_resolved = skip.resolve()
        except OSError:
            skip_resolved = skip

    promoted = 0
    for db_path in sorted(journals_dir.glob("*.sqlite")):
        try:
            linked = db_path.is_symlink()
        except OSError:
            continue
        if linked:
            continue
        if _is_skipped(db_path, skip_resolved):
            continue
        if _crashed_state(db_path) != "crashed":
            continue
        if _promote_one(root, db_path):
            promoted += 1
    return promoted


def _is_skipped(db_path: Path, skip_resolved: Path | None) -> bool:
    """True if *db_path* is the caller-owned journal we must never promote."""
    if skip_resolved is None:
        return False
    try:
        return db_path.resolve() == skip_resolved
    except OSError:
        return True


def _promote_one(root: Path, db_path: Path) -> bool:
    """Copy one crashed journal to ``crash-dumps/`` and remove it; True on success."""
    crash_path: Path | None = None
    artifact_root: Path | None = None
    try:
        with journal_file_claim(db_path, blocking=False) as claimed:
            if not claimed or _crashed_state(db_path) != "crashed":
                return False
            if db_path.is_symlink():
                return False
            crash_path, artifact_root = reserve_crash_dump_paths(root, db_path.stem)
            _copy_journal_to_crash_dump(db_path, crash_path)
            store = FilesystemArtifactStore(db_path.stem, data_dir=root)
            try:
                if not store._prepare_journal_retirement():
                    raise OSError("Could not prepare artifact journal retirement")
            finally:
                store.close()
            if not snapshot_crash_dump_artifacts(root, db_path, artifact_root):
                raise OSError("Crash artifact snapshot was incomplete")
            if _remove_journal(db_path):
                store = FilesystemArtifactStore(db_path.stem, data_dir=root)
                try:
                    store._complete_journal_retirement()
                finally:
                    store.close()
                logger.info("Swept crashed journal %s -> %s", db_path, crash_path)
                return True
            # If the source database itself remains, this promotion is only a
            # duplicate snapshot. Discard it so repeated sweeps cannot reserve
            # unbounded numeric suffixes for one unremovable journal. If only a
            # sidecar remains, retain the dump because it is the sole DB copy.
            if db_path.exists():
                discard_crash_dump(crash_path, artifact_root)
    except (OSError, sqlite3.DatabaseError):
        if crash_path is not None and artifact_root is not None:
            discard_crash_dump(crash_path, artifact_root)
        logger.warning("Failed to promote crashed journal %s", db_path, exc_info=True)
        return False
    return False


def _retry_artifact_retirements(root: Path, *, skip: Path | None) -> None:
    """Finish durable live-store retirements whose source journal is gone."""
    journals_dir = root / "journals"
    skip_resolved: Path | None = None
    if skip is not None:
        try:
            skip_resolved = skip.resolve()
        except OSError:
            skip_resolved = skip

    for session_id in FilesystemArtifactStore._pending_journal_retirements(root):
        db_path = journals_dir / f"{session_id}.sqlite"
        if _is_skipped(db_path, skip_resolved):
            continue
        try:
            with journal_file_claim(db_path, blocking=False) as claimed:
                if not claimed or db_path.exists():
                    continue
                store = FilesystemArtifactStore(session_id, data_dir=root)
                try:
                    store._complete_journal_retirement()
                finally:
                    store.close()
        except OSError:
            logger.debug(
                "Deferred artifact journal retirement failed for %s",
                session_id,
                exc_info=True,
            )


def _remove_journal(db_path: Path) -> bool:
    """Delete *db_path* and its WAL/SHM sidecars; False on failure."""
    try:
        if db_path.is_symlink():
            return False
        if not db_path.exists():
            return False
        db_path.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()
    except OSError:
        logger.warning("Failed to remove swept journal %s", db_path, exc_info=True)
        return False
    return True
