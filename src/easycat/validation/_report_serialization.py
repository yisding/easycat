"""Deterministic, redaction-aware validation report serialization."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import singledispatch
from typing import Any

from easycat.validation._report_models import ArtifactRef, ValidationEnvironment
from easycat.validation.redaction import redact_text, redact_value, should_redact_key

_GENERATED_RUN_PATH_SEGMENT_RE = re.compile(
    r"(?<=/runs/)\d{8}T\d{6}Z-(?:quick|socket|stress|contracts|live|release|latency-[^/]+)-[^/]+"
)


def serialize_dataclass(
    value: Any,
    *,
    include_none: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for item in fields(value):
        field_value = getattr(value, item.name)
        if field_value is None and item.name not in include_none:
            continue
        payload[item.name] = _serialize_value(field_value, item.name)
    return payload


def _serialize_value(value: Any, key: str | None = None) -> Any:
    if key == "command":
        return redact_value(value, key)
    if should_redact_key(key) and not is_dataclass(value):
        return redact_value(value, key)
    return _serialize_typed_value(value, key)


@singledispatch
def _serialize_typed_value(value: Any, key: str | None) -> Any:
    if not is_dataclass(value):
        return value
    return {
        field_name: field_value
        for field_name, field_value in serialize_dataclass(value).items()
        if not _is_empty_optional(field_value)
    }


@_serialize_typed_value.register
def _serialize_enum(value: StrEnum, key: str | None) -> str:
    return value.value


@_serialize_typed_value.register
def _serialize_datetime(value: datetime, key: str | None) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    return value.isoformat().replace("+00:00", "Z")


@_serialize_typed_value.register
def _serialize_text(value: str, key: str | None) -> str:
    return redact_value(value, key)


@_serialize_typed_value.register(type(None))
@_serialize_typed_value.register(bool)
@_serialize_typed_value.register(int)
@_serialize_typed_value.register(float)
def _serialize_scalar(value: bool | int | float | None, key: str | None) -> Any:
    return value


@_serialize_typed_value.register
def _serialize_artifact(value: ArtifactRef, key: str | None) -> dict[str, str]:
    return {"kind": redact_text(value.kind), "path": _redact_artifact_path(value.path)}


@_serialize_typed_value.register
def _serialize_environment(value: ValidationEnvironment, key: str | None) -> dict[str, Any]:
    return {
        "python": redact_text(value.python),
        "platform": redact_text(value.platform),
        "ci": value.ci,
        "env_vars": {
            str(name): bool(present)
            for name, present in sorted(value.env_vars.items(), key=lambda item: str(item[0]))
        },
    }


@_serialize_typed_value.register(Mapping)
def _serialize_mapping(value: Mapping[Any, Any], key: str | None) -> dict[str, Any]:
    return {
        str(item_key): _serialize_value(item_value, str(item_key))
        for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
        if not _is_empty_optional(item_value)
    }


@_serialize_typed_value.register(Sequence)
def _serialize_sequence(value: Sequence[Any], key: str | None) -> list[Any]:
    return [_serialize_value(item, key) for item in value]


def _redact_artifact_path(value: str) -> str:
    generated_segments: list[str] = []

    def preserve_generated_segment(match: re.Match[str]) -> str:
        generated_segments.append(match.group(0))
        return f"__EASYCAT_VALIDATION_RUN_SEGMENT_{len(generated_segments) - 1}__"

    redacted = redact_text(_GENERATED_RUN_PATH_SEGMENT_RE.sub(preserve_generated_segment, value))
    for index, segment in enumerate(generated_segments):
        redacted = redacted.replace(f"__EASYCAT_VALIDATION_RUN_SEGMENT_{index}__", segment)
    return redacted


def _is_empty_optional(value: Any) -> bool:
    return value is None or value == {} or value == ()
