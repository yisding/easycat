"""Annotation sidecar for read-only debug bundles.

A reviewer triaging a captured call wants to record a verdict — did the
turn pass or fail, what kind of failure was it, a 1-5 quality score, and a
free-text note — without ever mutating the bundle or its journal.  The
journal behind a served bundle is opened ``mode=ro`` (see
``runtime/journal_views.py``), so an append is a silent no-op; writing the
verdict back into it would be a lie.

Instead, verdicts live in a JSON *sidecar* next to the bundle:
``<bundle>.annotations.json``.  The schema is a flat ``{turn_id: record}``
map keyed by turn so the SPA and the ``bundles show`` CLI can hydrate a
per-turn verdict in O(1).  Writes are read-modify-write with an atomic
temp-file rename (mirroring ``debug/export.py``) so a crash mid-write can
never corrupt an existing sidecar.

The sidecar is the single source of the failure-type taxonomy
(:data:`FAILURE_TYPES`); the SPA hard-codes the same six strings and a
parity test keeps them in lockstep.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Failure-type taxonomy shared with the SPA annotation control and the
# troubleshooting/explain surface.  Keep this tuple and the hard-coded JS
# list in ``debugger/static/index.html`` in lockstep (a static-asset parity
# test enforces it).
FAILURE_TYPES: tuple[str, ...] = (
    "asr_error",
    "barge_in_miss",
    "tts_cutoff",
    "wrong_tool",
    "hallucination",
    "self_echo",
)

# Bump if the on-disk shape changes; loaders tolerate older/unknown values.
SCHEMA_VERSION = 1

# Cap on the free-text note so a runaway client can't write an unbounded
# blob into the sidecar.
_MAX_NOTES_LEN = 4000

# Keep annotation sidecars bounded: callers read and rewrite the whole JSON
# envelope, so cap both record count and serialized bytes.
MAX_ANNOTATIONS = 1000
MAX_SIDECAR_BYTES = 5 * 1024 * 1024

# Valid score band (inclusive).  ``None`` means "no score given".
_SCORE_MIN = 1
_SCORE_MAX = 5

# ``flock`` semantics are platform-specific for multiple descriptors opened
# by one process.  Serialize local writers explicitly and layer a separate
# lock file on top for writers in other debugger processes.  The lock must
# not be the sidecar itself: every save atomically replaces that file.
_LOCAL_WRITE_LOCK = threading.RLock()
_READ_CHUNK_SIZE = 64 * 1024


class AnnotationError(ValueError):
    """Raised when an :class:`Annotation` field fails validation."""


@dataclass
class Annotation:
    """One reviewer verdict about a single turn.

    ``passed`` is a tri-state: ``True`` (pass), ``False`` (fail), or
    ``None`` (no verdict yet).  ``failure_type`` must be one of
    :data:`FAILURE_TYPES` or ``None``; ``score`` is ``1..5`` or ``None``;
    ``notes`` is free text capped at 4000 chars.
    """

    turn_id: str
    passed: bool | None = None
    failure_type: str | None = None
    score: int | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.turn_id, str) or not self.turn_id:
            raise AnnotationError("turn_id must be a non-empty string")
        if self.passed is not None and not isinstance(self.passed, bool):
            raise AnnotationError("passed must be a bool or None")
        if self.failure_type is not None and self.failure_type not in FAILURE_TYPES:
            raise AnnotationError(
                f"failure_type must be one of {FAILURE_TYPES} or None, got {self.failure_type!r}"
            )
        if self.score is not None:
            # ``bool`` is an ``int`` subclass; reject it explicitly so a
            # stray ``True`` can't masquerade as a score of 1.
            if isinstance(self.score, bool) or not isinstance(self.score, int):
                raise AnnotationError("score must be an int or None")
            if not (_SCORE_MIN <= self.score <= _SCORE_MAX):
                raise AnnotationError(
                    f"score must be between {_SCORE_MIN} and {_SCORE_MAX}, got {self.score}"
                )
        if not isinstance(self.notes, str):
            raise AnnotationError("notes must be a string")
        if len(self.notes) > _MAX_NOTES_LEN:
            raise AnnotationError(f"notes must be at most {_MAX_NOTES_LEN} characters")

    def to_record(self) -> dict[str, Any]:
        """Project to the on-disk per-turn record (without ``updated_at``)."""
        return {
            "passed": self.passed,
            "failure_type": self.failure_type,
            "score": self.score,
            "notes": self.notes,
        }


def sidecar_path(bundle_path: str | Path) -> Path:
    """Return the sidecar path for *bundle_path*: ``<bundle>.annotations.json``.

    Appends the suffix to the full bundle name so ``call.zip`` maps to
    ``call.zip.annotations.json`` — never colliding with the bundle and
    obvious as a companion file in a directory listing.
    """
    path = Path(bundle_path)
    return path.with_name(path.name + ".annotations.json")


def _read_sidecar_text(path: Path) -> str | None:
    """Read a bounded regular sidecar without following symlinks.

    Annotation sidecars are advisory, so an unusual filesystem object is
    treated like corrupt input rather than allowed to stall a debugger route.
    Check both the path and the opened descriptor to close the common
    check-then-open race.
    """
    try:
        initial = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(initial.st_mode) or initial.st_size > MAX_SIDECAR_BYTES:
        return None

    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None

    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size > MAX_SIDECAR_BYTES
            or (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino)
        ):
            return None

        data = bytearray()
        while len(data) <= MAX_SIDECAR_BYTES:
            chunk = os.read(
                fd,
                min(_READ_CHUNK_SIZE, MAX_SIDECAR_BYTES + 1 - len(data)),
            )
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > MAX_SIDECAR_BYTES:
            return None
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)


def _lock_file(lock_file: Any) -> None:
    """Acquire an exclusive advisory lock for the open lock file."""
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        lock_file.write(b"\0")
        lock_file.flush()
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _unlock_file(lock_file: Any) -> None:
    """Release an exclusive advisory lock acquired by :func:`_lock_file`."""
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _sidecar_write_lock(path: Path) -> Iterator[None]:
    """Serialize a sidecar read-modify-write across threads and processes."""
    lock_path = path.with_name(path.name + ".lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    with _LOCAL_WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, flags, 0o600)
        with os.fdopen(fd, "r+b") as lock_file:
            if not stat.S_ISREG(os.fstat(lock_file.fileno()).st_mode):
                raise OSError("annotation sidecar lock is not a regular file")
            _lock_file(lock_file)
            try:
                yield
            finally:
                _unlock_file(lock_file)


def load_annotations(bundle_path: str | Path) -> dict[str, dict[str, Any]]:
    """Load the ``{turn_id: record}`` map for *bundle_path*.

    Tolerates a missing or corrupt sidecar by returning an empty map — a
    reviewer's verdicts are advisory, never load-bearing, so a bad file
    must never break ``bundles show`` or the debugger UI.
    """
    path = sidecar_path(bundle_path)
    raw = _read_sidecar_text(path)
    if raw is None:
        return {}
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    annotations = payload.get("annotations")
    if not isinstance(annotations, dict):
        return {}
    # Mirror the byte cap: a sidecar with more records than the write path
    # ever produces is treated as corrupt so downstream consumers stay
    # bounded and ``save_annotation`` is not locked out by the count cap.
    if len(annotations) > MAX_ANNOTATIONS:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for turn_id, record in annotations.items():
        if isinstance(turn_id, str) and isinstance(record, dict):
            out[turn_id] = record
    return out


def save_annotation(
    bundle_path: str | Path,
    annotation: Annotation,
    *,
    allowed_turn_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Persist *annotation* into the sidecar, returning its stored record.

    Read-modify-write: load the existing map, upsert the record for
    ``annotation.turn_id`` (stamping ``updated_at``), and write the whole
    file back atomically via a temp file + rename so a crash mid-write
    leaves the prior sidecar intact.  Never touches the bundle or its
    journal.
    """
    if allowed_turn_ids is not None and annotation.turn_id not in allowed_turn_ids:
        raise AnnotationError(f"turn_id does not exist in bundle: {annotation.turn_id!r}")

    path = sidecar_path(bundle_path)
    with _sidecar_write_lock(path):
        existing = load_annotations(bundle_path)
        is_new_turn = annotation.turn_id not in existing
        if is_new_turn and len(existing) >= MAX_ANNOTATIONS:
            raise AnnotationError(f"annotation sidecar is limited to {MAX_ANNOTATIONS} turns")
        record = annotation.to_record()
        record["updated_at"] = datetime.now(UTC).isoformat()
        existing[annotation.turn_id] = record
        payload = {"schema_version": SCHEMA_VERSION, "annotations": existing}
        serialized = json.dumps(payload, indent=2)
        if len(serialized.encode("utf-8")) > MAX_SIDECAR_BYTES:
            raise AnnotationError(f"annotation sidecar is limited to {MAX_SIDECAR_BYTES} bytes")

        tmp_name: str | None = None
        try:
            tmp = tempfile.NamedTemporaryFile(
                dir=path.parent, suffix=".tmp", delete=False, mode="w", encoding="utf-8"
            )
            tmp_name = tmp.name
            try:
                tmp.write(serialized)
            finally:
                tmp.close()
            Path(tmp_name).replace(path)
        except Exception:
            if tmp_name and Path(tmp_name).exists():
                Path(tmp_name).unlink()
            raise
    return record
