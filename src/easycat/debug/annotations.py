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
import tempfile
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

# Valid score band (inclusive).  ``None`` means "no score given".
_SCORE_MIN = 1
_SCORE_MAX = 5


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


def load_annotations(bundle_path: str | Path) -> dict[str, dict[str, Any]]:
    """Load the ``{turn_id: record}`` map for *bundle_path*.

    Tolerates a missing or corrupt sidecar by returning an empty map — a
    reviewer's verdicts are advisory, never load-bearing, so a bad file
    must never break ``bundles show`` or the debugger UI.
    """
    path = sidecar_path(bundle_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    annotations = payload.get("annotations")
    if not isinstance(annotations, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for turn_id, record in annotations.items():
        if isinstance(turn_id, str) and isinstance(record, dict):
            out[turn_id] = record
    return out


def save_annotation(bundle_path: str | Path, annotation: Annotation) -> dict[str, Any]:
    """Persist *annotation* into the sidecar, returning its stored record.

    Read-modify-write: load the existing map, upsert the record for
    ``annotation.turn_id`` (stamping ``updated_at``), and write the whole
    file back atomically via a temp file + rename so a crash mid-write
    leaves the prior sidecar intact.  Never touches the bundle or its
    journal.
    """
    path = sidecar_path(bundle_path)
    existing = load_annotations(bundle_path)
    record = annotation.to_record()
    record["updated_at"] = datetime.now(UTC).isoformat()
    existing[annotation.turn_id] = record
    payload = {"schema_version": SCHEMA_VERSION, "annotations": existing}

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        tmp = tempfile.NamedTemporaryFile(
            dir=path.parent, suffix=".tmp", delete=False, mode="w", encoding="utf-8"
        )
        tmp_name = tmp.name
        try:
            json.dump(payload, tmp, indent=2)
        finally:
            tmp.close()
        Path(tmp_name).replace(path)
    except Exception:
        if tmp_name and Path(tmp_name).exists():
            Path(tmp_name).unlink()
        raise
    return record


def merged_tags(record: dict[str, Any], annotation: dict[str, Any] | None) -> list[str]:
    """Union a journal record's tags with an annotation-derived tag set.

    Stub for the ``bundles list --tag`` / ``journal --tag`` union filter,
    which is gated on the WP8 tag-slice work.  Currently returns only the
    record's own ``tags`` so callers wire against a stable signature; the
    annotation-derived tags (e.g. ``fail``, ``failure:<type>``) are folded
    in once the tag-slice CLI lands.
    """
    tags = record.get("tags")
    if isinstance(tags, list):
        return [str(t) for t in tags]
    return []
