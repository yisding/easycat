"""Allowlisted journal projection for shareable coding-agent context packs."""

from __future__ import annotations

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
_CONTEXT_ERROR_KEYS = frozenset(("type", "code", "status"))
_IDENTITY_KEYS = ("sequence", "kind", "name", "session_id", "turn_id")
_REFERENCE_KEYS = ("input_ref", "output_ref")
_EMPTY_VALUES: tuple[object, ...] = (None, "", [], {})


def _project_error(error: object) -> dict[str, Any] | None:
    if not isinstance(error, Mapping):
        return None

    projected = {
        str(key): redact_value(error[key], str(key))
        for key in sorted(error, key=str)
        if str(key) in _CONTEXT_ERROR_KEYS and error[key] not in _EMPTY_VALUES
    }
    omitted = sum(
        1
        for key, value in error.items()
        if str(key) not in _CONTEXT_ERROR_KEYS and value not in _EMPTY_VALUES
    )
    if omitted:
        projected["omitted_error_fields"] = omitted
    return projected or None


def _project_data(data: object) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        return {"omitted_data_fields": 1} if data not in _EMPTY_VALUES else {}

    safe_data = {
        str(key): redact_value(data[key], str(key))
        for key in sorted(data, key=str)
        if str(key) in _CONTEXT_DATA_KEYS
    }
    projected: dict[str, Any] = {"data": safe_data} if safe_data else {}
    omitted = len(data) - len(safe_data)
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
