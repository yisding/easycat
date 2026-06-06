from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SchemaDriftStatus = Literal["unchanged", "additive_warning", "breaking_failure", "unknown"]
SchemaDirection = Literal["inbound", "outbound"]


@dataclass(frozen=True)
class DirectionalSchemaRule:
    required_fields: frozenset[str] = frozenset()
    optional_fields: frozenset[str] = frozenset()
    enum_fields: dict[str, frozenset[str]] = field(default_factory=dict)
    object_required_fields: dict[str, frozenset[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class SchemaFingerprintRule:
    inbound: DirectionalSchemaRule | None = None
    outbound: DirectionalSchemaRule | None = None


def compare_schema_fingerprint(
    observed: dict[str, Any],
    rule: SchemaFingerprintRule,
    *,
    direction: SchemaDirection | str,
) -> dict[str, Any]:
    selected = _select_directional_rule(rule, direction)
    if selected is None:
        return {
            "status": "unknown",
            "reason": f"no schema fingerprint rule for {direction!r}",
        }

    observed_fields = set(observed)
    top_level_required = selected.required_fields | set(selected.object_required_fields)
    missing_required = sorted(top_level_required - observed_fields)
    enum_failures = {
        field: observed[field]
        for field, allowed in selected.enum_fields.items()
        if field in observed and observed[field] not in allowed
    }
    object_shape_failures = _object_shape_failures(observed, selected)
    if missing_required or enum_failures or object_shape_failures:
        return {
            "status": "breaking_failure",
            "missing_required_fields": missing_required,
            "enum_failures": enum_failures,
            "object_shape_failures": object_shape_failures,
        }

    known_fields = (
        selected.required_fields
        | selected.optional_fields
        | set(selected.enum_fields)
        | set(selected.object_required_fields)
    )
    additive_fields = sorted(observed_fields - known_fields)
    if additive_fields:
        return {
            "status": "additive_warning",
            "additive_fields": additive_fields,
        }
    return {
        "status": "unchanged",
        "additive_fields": [],
        "missing_required_fields": [],
        "enum_failures": {},
        "object_shape_failures": {},
    }


def _select_directional_rule(
    rule: SchemaFingerprintRule,
    direction: SchemaDirection | str,
) -> DirectionalSchemaRule | None:
    if direction == "inbound":
        return rule.inbound
    if direction == "outbound":
        return rule.outbound
    return None


def _object_shape_failures(
    observed: dict[str, Any],
    selected: DirectionalSchemaRule,
) -> dict[str, dict[str, Any]]:
    failures: dict[str, dict[str, Any]] = {}
    for field_name, required_fields in selected.object_required_fields.items():
        if field_name not in observed:
            continue
        value = observed[field_name]
        if not isinstance(value, dict):
            failures[field_name] = {
                "expected": "object",
                "actual": type(value).__name__,
            }
            continue
        missing_nested = sorted(required_fields - set(value))
        if missing_nested:
            failures[field_name] = {"missing_required_fields": missing_nested}
    return failures
