"""JSON-safe serialization for journal records and config snapshots.

Single source of truth for the *JSON shape* of debug data — the mirror of
:mod:`easycat.runtime._journal_codec`, which owns the persisted SQL-row shape.
Both the export bundle (:mod:`easycat.debug.export`) and the live debugger
server (:mod:`easycat.debugger`) coerce ``JournalRecord`` objects and config
snapshots into JSON dicts. Keeping one *generic dataclass walk* here stops the
live view and the exported bundle from rendering the same record differently:
before consolidation the server's hardcoded attribute tuple dropped ``tags``
and record-subclass fields (``framework`` / ``direction`` /
``bridge_latency_ms`` …) that the export walk already included, so the same
record looked different live versus in a bundle. The generic walk is canonical.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any


def json_safe_value(value: Any) -> Any:
    """Recursively coerce *value* into a JSON-serializable structure.

    Enums collapse to their ``.value``; dataclasses and plain objects walk
    their public fields; frozensets/sets sort for stable output; bytes decode
    as UTF-8 (replacement on error). This generic walk is what both the live
    debugger and the export bundle serialize through, so a record renders
    identically in each.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "value") and not isinstance(value, (str, bytes, int, float, bool)):
        return json_safe_value(value.value)
    if dataclasses.is_dataclass(value):
        return {
            field.name: json_safe_value(getattr(value, field.name))
            for field in dataclasses.fields(value)
            if not field.name.startswith("_")
        }
    if isinstance(value, Mapping):
        return {str(k): json_safe_value(v) for k, v in value.items()}
    if isinstance(value, frozenset):
        return sorted((json_safe_value(v) for v in value), key=repr)
    if isinstance(value, set):
        return sorted((json_safe_value(v) for v in value), key=repr)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [json_safe_value(v) for v in value]
    if hasattr(value, "__dict__"):
        return {k: json_safe_value(v) for k, v in value.__dict__.items() if not k.startswith("_")}
    return value


def record_to_dict(record: Any) -> dict[str, Any]:
    """Convert a JournalRecord-like object to a JSON-friendly dict.

    Runs the generic :func:`json_safe_value` walk so every field — including
    ``tags`` and record-subclass fields — is preserved. Falls back to the
    original object only when the walk does not yield a dict (never expected for
    a real record).
    """
    value = json_safe_value(record)
    return value if isinstance(value, dict) else record


def safe_config_snapshot_from_session(session: Any) -> dict[str, Any]:
    """Return the allowlisted, secret-redacted config snapshot for *session*.

    Prefers ``_easycat_config`` (the original user-facing config) over
    ``_config`` (the wired SessionConfig holding live provider instances) so the
    snapshot captures meaningful settings like debug mode, journal backend, and
    turn-taking policy instead of ``<object at 0x…>`` repr strings.
    """
    try:
        from easycat.runtime.safe_defaults import safe_config_snapshot
    except ImportError:
        return {}
    config = getattr(session, "_easycat_config", None) or getattr(session, "_config", None)
    if config is None:
        return {}
    return safe_config_snapshot(config)


__all__ = [
    "json_safe_value",
    "record_to_dict",
    "safe_config_snapshot_from_session",
]
