"""Allowlisted journal projection for shareable coding-agent context packs."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

from easycat.debug._turn_timeline import record_wall_ns
from easycat.validation.redaction import redact_value

_CONTEXT_DATA_KEYS = frozenset(
    {
        "boundary_reason",
        "bridge_latency_ms",
        "call_id",
        "cancellation_mode",
        "cause",
        "caused_by_signal_id",
        "checkpoint_id",
        "committable",
        "direction",
        "display_name",
        "duration_ms",
        "exit_reason",
        "fidelity",
        "format",
        "framework",
        "from_unit",
        "handoff_reason",
        "latency_ms",
        "model",
        "mutation_kind",
        "observed_stage",
        "parent_unit_id",
        "phase",
        "provider",
        "provider_name",
        "sample_rate",
        "signal_id",
        "signal_kind",
        "stage",
        "to_unit",
        "tool_call_id",
        "tool_name",
        "transition_kind",
        "unit_id",
        "unit_kind",
    }
)
_CONTEXT_TOP_LEVEL_KEYS = _CONTEXT_DATA_KEYS | frozenset(
    {
        "framework",
        "direction",
        "bridge_latency_ms",
    }
)
_CONTEXT_ERROR_KEYS = frozenset(("type", "code", "status", "notes"))
_CONTEXT_ERROR_NOTE_KEYS = frozenset(("stage", "provider", "elapsed_ms", "sequence", "record_key"))
_CONTEXT_ERROR_NOTE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_CONTEXT_ERROR_RECORD_KEY = re.compile(r"^cp_(?:0|[1-9][0-9]*)$")
_IDENTITY_KEYS = ("sequence", "kind", "name", "session_id", "turn_id")
_REFERENCE_KEYS = ("input_ref", "output_ref")
_EMPTY_VALUES: tuple[object, ...] = (None, "", [], {})


def _project_allowlisted(
    mapping: Mapping[object, Any],
    allowed_keys: frozenset[str],
) -> tuple[dict[str, Any], int]:
    projected: dict[str, Any] = {}
    omitted = 0
    for key in sorted(mapping, key=str):
        value = mapping[key]
        if value in _EMPTY_VALUES:
            continue
        normalized_key = str(key)
        if normalized_key not in allowed_keys:
            omitted += 1
            continue
        projected[normalized_key] = redact_value(value, normalized_key)
    return projected, omitted


def _project_error_note(key: str, value: str) -> str | int | float | None:
    if key in {"stage", "provider"}:
        if not _CONTEXT_ERROR_NOTE_TOKEN.fullmatch(value):
            return None
        redacted = redact_value(value, key)
        return redacted if isinstance(redacted, str) else None
    if key == "elapsed_ms":
        try:
            elapsed_ms = float(value)
        except ValueError:
            return None
        return elapsed_ms if math.isfinite(elapsed_ms) and elapsed_ms >= 0 else None
    if key == "sequence":
        try:
            sequence = int(value)
        except ValueError:
            return None
        return sequence if sequence >= 0 and value == str(sequence) else None
    if key == "record_key":
        return value if _CONTEXT_ERROR_RECORD_KEY.fullmatch(value) else None
    return None


def _project_error_notes(notes: object) -> tuple[dict[str, str | int | float], int]:
    if not isinstance(notes, str):
        return {}, 1

    projected: dict[str, str | int | float] = {}
    omitted = 0
    for line in notes.splitlines():
        key, separator, value = line.partition("=")
        if (
            not separator
            or key not in _CONTEXT_ERROR_NOTE_KEYS
            or key in projected
            or (safe_value := _project_error_note(key, value)) is None
        ):
            omitted += 1
            continue
        projected[key] = safe_value
    return projected, omitted


def _project_error(error: object) -> dict[str, Any] | None:
    if not isinstance(error, Mapping):
        return None

    projected, omitted = _project_allowlisted(error, _CONTEXT_ERROR_KEYS)
    raw_notes = error.get("notes")
    projected.pop("notes", None)
    if raw_notes not in _EMPTY_VALUES:
        notes, omitted_note_lines = _project_error_notes(raw_notes)
        if notes:
            projected["notes"] = notes
            if omitted_note_lines:
                projected["omitted_error_note_lines"] = omitted_note_lines
        else:
            omitted += 1
    if omitted:
        projected["omitted_error_fields"] = omitted
    return projected or None


def _project_data(data: object) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        return {"omitted_data_fields": 1} if data not in _EMPTY_VALUES else {}

    safe_data, omitted = _project_allowlisted(data, _CONTEXT_DATA_KEYS)
    projected: dict[str, Any] = {"data": safe_data} if safe_data else {}
    if omitted:
        projected["omitted_data_fields"] = omitted
    return projected


def _project_references(record: Mapping[str, Any]) -> dict[str, Any]:
    refs = {key: redact_value(record[key], key) for key in _REFERENCE_KEYS if record.get(key)}
    return {"refs": refs} if refs else {}


def _project_tags(tags: object) -> dict[str, Any]:
    if not isinstance(tags, list | tuple | set | frozenset) or not tags:
        return {}
    return {"tags": redact_value(sorted(tags, key=str), "tags")}


def project_context_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the allowlisted, redacted representation of one journal record."""
    projected = {
        key: redact_value(record[key], key)
        for key in _IDENTITY_KEYS
        if record.get(key) not in (None, "")
    }

    wall_ns = record_wall_ns(record)
    if wall_ns is not None:
        projected["wall_ns"] = wall_ns

    projected.update(_project_data(record.get("data")))
    projected.update(
        {
            key: redact_value(record[key], key)
            for key in sorted(_CONTEXT_TOP_LEVEL_KEYS)
            if key in record and key not in projected
        }
    )
    projected.update(_project_references(record))

    error = _project_error(record.get("error"))
    if error:
        projected["error"] = error
    projected.update(_project_tags(record.get("tags")))
    return projected


def project_context_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project journal records across the sharing boundary."""
    return [project_context_record(record) for record in records]
