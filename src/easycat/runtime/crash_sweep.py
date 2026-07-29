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
import shutil
import sqlite3
from pathlib import Path

from easycat.runtime._private_files import chmod_private_file, mkdir_private

logger = logging.getLogger(__name__)


def _process_birth_identity(pid: int) -> str | None:
    """Return a boot-scoped process start identity where the OS exposes one."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields_after_comm = stat[stat.rfind(")") + 2 :].split()
        start_ticks = fields_after_comm[19]
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except (IndexError, OSError):
        return None
    if not boot_id or not start_ticks:
        return None
    return f"{boot_id}:{start_ticks}"


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
    main database before the byte copy — with ``wal_autocheckpoint=0`` recent
    records may live only in the WAL, and a bare copy would lose them.
    """
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except sqlite3.OperationalError:
        pass  # Best-effort; copy WAL sidecars below as a fallback.

    shutil.copy2(str(db_path), str(crash_path))
    chmod_private_file(crash_path)
    # Also copy WAL/SHM sidecars if the checkpoint was incomplete (e.g. a
    # concurrent reader held the file) so no committed page is lost.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            crash_sidecar = Path(str(crash_path) + suffix)
            shutil.copy2(str(sidecar), str(crash_sidecar))
            chmod_private_file(crash_sidecar)


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

    1. A ``live_pid`` marker (written on journal open, cleared on clean
       close).  If that PID is still running, the journal is live -> skip.
       This catches the idle-but-live window a lock probe would miss.
    2. A ``BEGIN IMMEDIATE`` write-lock probe on a would-be crash, as a
       backstop for an actively-writing session: if the lock is held, skip.
    """
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
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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

    1. A ``live_pid`` marker (written on journal open, cleared on clean
       close).  If that PID names a running process, the journal is live.
       This catches the idle-but-live window a lock probe alone would miss.
    2. A ``BEGIN IMMEDIATE`` write-lock probe as a backstop for an
       actively-writing session whose ``live_pid`` marker might be stale
       (PID reuse): if the lock cannot be taken, treat the journal as live.

    Any file we cannot open or read read-only is treated as live (returns
    ``True``) so retention errs on the side of preservation rather than
    deleting a database we simply failed to classify.

    Classification is **read-only first** (it never opens a cleanly-closed or
    foreign journal for writing, which would checkpoint and rewrite its WAL
    sidecar).  Only a journal that lacks both a clean-close marker and a live
    PID gets the final ``BEGIN IMMEDIATE`` write-lock probe — the same gate
    the crash sweep uses — so a kept-but-idle valid journal is never mutated.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
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

    # No live-PID marker and no clean-close marker: an actively-writing
    # session may hold the lock with a stale/absent marker.  Backstop with a
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
    crash_dir = root / "crash-dumps"
    crash_path = crash_dir / f"{db_path.stem}.sqlite"
    try:
        mkdir_private(crash_dir)
        _copy_journal_to_crash_dump(db_path, crash_path)
    except OSError:
        logger.warning("Failed to promote crashed journal %s", db_path, exc_info=True)
        return False
    if _remove_journal(db_path):
        logger.info("Swept crashed journal %s -> %s", db_path, crash_path)
        return True
    return False


def _remove_journal(db_path: Path) -> bool:
    """Delete *db_path* and its WAL/SHM sidecars; False on failure."""
    try:
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
