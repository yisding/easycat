"""Secret-safe serialization for framework-state artifacts."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Any

from easycat.validation.redaction import (
    REDACTED_SECRET,
    redact_text,
    redact_value,
    should_redact_secret_key,
)


def _mapping_key_name(key: Any) -> str | None:
    """Return a safe JSON key without invoking opaque-object string hooks."""
    if type(key) is str:
        return key
    if key is None or type(key) in (bool, int, float):
        return str(key)
    return None


def _is_secret_association(value: Any) -> bool:
    """Recognize only two-item sequences explicitly naming a secret field."""
    return (
        isinstance(value, Sequence)
        and not isinstance(value, str | bytes | bytearray)
        and len(value) == 2
        and type(value[0]) is str
        and should_redact_secret_key(value[0])
    )


def _has_opaque_primitive_association_key(value: Any) -> bool:
    """Detect primitive subclasses whose hooks must not inspect pair values."""
    if (
        not isinstance(value, Sequence)
        or isinstance(value, str | bytes | bytearray)
        or len(value) != 2
    ):
        return False
    key = value[0]
    return isinstance(key, str | bool | int | float) and type(key) not in (
        str,
        bool,
        int,
        float,
    )


def _remove_secret_shaped_keys(value: Any) -> Any:
    """Drop mapping entries whose key itself contains credential material."""
    if isinstance(value, Mapping):
        scrubbed: dict[str, Any] = {}
        for key, item in value.items():
            name = _mapping_key_name(key)
            if name is None:
                continue
            if redact_text(name, policy="secrets") != name:
                continue
            if should_redact_secret_key(name):
                scrubbed[name] = REDACTED_SECRET
                continue
            scrubbed[name] = _remove_secret_shaped_keys(item)
        return scrubbed
    if _is_secret_association(value):
        return {value[0]: REDACTED_SECRET}
    if _has_opaque_primitive_association_key(value):
        return "[UNSERIALIZABLE]"
    if isinstance(value, AbstractSet):
        scrubbed_items = [_remove_secret_shaped_keys(item) for item in value]
        return sorted(scrubbed_items, key=_canonical_json_key)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_remove_secret_shaped_keys(item) for item in value]
    return value


def serialize_framework_state(value: Any, *, fallback: bytes = b"{}") -> bytes:
    """Return JSON artifact bytes with credential keys and values scrubbed.

    Framework state is ultimately copied verbatim into debug bundles, so it
    needs the same secrets-only value policy as journal records. Top-level
    credential fields are omitted to preserve the bridge snapshot contract;
    nested credential fields remain visible as redaction markers so the state
    shape is still useful when debugging.
    """
    try:
        if isinstance(value, Mapping):
            scrubbed = {}
            for key, item in value.items():
                name = _mapping_key_name(key)
                if (
                    name is None
                    or should_redact_secret_key(name)
                    or redact_text(name, policy="secrets") != name
                ):
                    continue
                scrubbed[name] = redact_value(
                    _remove_secret_shaped_keys(item),
                    name,
                    policy="secrets",
                )
        else:
            scrubbed = redact_value(_remove_secret_shaped_keys(value), policy="secrets")
        return json.dumps(
            scrubbed,
            default=_redacted_string,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        ).encode("utf-8")
    except Exception:  # noqa: BLE001 intentional best-effort artifact boundary
        return fallback


def _redacted_string(value: Any) -> str:
    """Represent opaque objects without serializing their potentially secret repr."""
    _ = value
    return "[UNSERIALIZABLE]"


def _canonical_json_key(value: Any) -> str:
    """Return a stable ordering key for already-scrubbed unordered items."""
    return json.dumps(
        value,
        default=_redacted_string,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
