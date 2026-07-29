"""Config, environment, and journal safety defaults.

Hard-coded allowlists keep obvious credentials out of generated config and
environment metadata. The journal write filter always scrubs secrets and can
optionally scrub PII, while its default deliberately preserves normal
transcript, agent-output, and tool-result text so replay remains useful.
Journal records and exported bundles therefore remain sensitive.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field, is_dataclass, replace
from dataclasses import fields as dc_fields
from enum import Enum
from itertools import islice
from typing import Any
from urllib.parse import urlparse

from easycat.runtime.records import ErrorInfo, JournalRecord
from easycat.validation.redaction import RedactionPolicy, redact_text, redact_value

# ── Config field allowlist ────────────────────────────────────────

SAFE_CONFIG_FIELDS: frozenset[str] = frozenset(
    {
        # Provider kind identifiers (not credentials)
        "stt",
        "tts",
        "vad",
        "noise_reduction",
        "echo_cancellation",
        # Turn/pipeline policy
        "turn_taking",
        "smart_turn",
        "smart_turn_sensitivity",
        "timeouts",
        "debug",
        "warmup",
        # Pipeline flags
        "capture_audio",
        "enable_noise_reduction",
        "enable_echo_cancellation",
        "enable_vad",
        "auto_turn_from_stt_final",
        "strip_markdown",
        "interruption_mode",
        "on_agent_failure",
        # Journal config (safe to report)
        "journal_backend",
        "journal_capacity",
        "journal_redaction",
        "journal_retention",
    }
)

# Secret-adjacent field name fragments — any config field whose name
# contains one of these is unconditionally excluded, even if someone
# accidentally adds it to the allowlist above.
_SECRET_FRAGMENTS: frozenset[str] = frozenset(
    {
        "key",
        "secret",
        "token",
        "password",
        "credential",
        "auth",
    }
)

# ── Environment variable allowlist ────────────────────────────────

SAFE_ENV_VARS: frozenset[str] = frozenset(
    {
        # EasyCat runtime control
        "EASYCAT_DEBUG",
        "EASYCAT_DATA_DIR",
        # Journal backend adapters (presence only — values may contain paths)
        "EASYCAT_JOURNAL_LITESTREAM_REPLICA",
        "EASYCAT_LIBSQL_URL",
        # Deployment identification (non-secret, useful for bundles)
        "HOSTNAME",
        "REGION",
        "DEPLOY_ENV",
    }
)

# Vars whose values are URLs that may embed credentials or signed query
# params.  ``safe_env_snapshot`` reduces these to ``scheme://host`` form.
_URL_VALUED_VARS: frozenset[str] = frozenset(
    {
        "EASYCAT_JOURNAL_LITESTREAM_REPLICA",
        "EASYCAT_LIBSQL_URL",
    }
)

# A reprlib-style policy with an additional global node/output budget. Values
# are diagnostics, so omission is preferable to recursive or unbounded export.
_SAFE_REPR_MAX_DEPTH = 6
_SAFE_REPR_MAX_ITEMS = 16
_SAFE_REPR_MAX_NODES = 128
_SAFE_REPR_MAX_SCALAR_CHARS = 256
_SAFE_REPR_MAX_OUTPUT_CHARS = 8_192
_SAFE_REPR_OMITTED = "..."
_SAFE_REPR_UNAVAILABLE = "<unavailable>"


# ── Snapshot helpers ──────────────────────────────────────────────


def _is_secret_name(name: str) -> bool:
    if len(name) > _SAFE_REPR_MAX_SCALAR_CHARS:
        return True
    lower = name.lower()
    return any(frag in lower for frag in _SECRET_FRAGMENTS)


def _safe_repr(val: Any) -> str:
    """Render a bounded, redacted config representation.

    Structured values are traversed without invoking arbitrary ``__repr__``
    methods. The renderer bounds recursion depth, collection items, visited
    nodes, scalar size, and final output size so cyclic or attacker-sized config
    objects cannot exhaust snapshot and debug-export paths.
    """
    rendered = _SafeRenderer().render(val)
    return _truncate_middle(rendered, _SAFE_REPR_MAX_OUTPUT_CHARS)


@dataclass(slots=True)
class _SafeRenderer:
    max_depth: int = _SAFE_REPR_MAX_DEPTH
    max_items: int = _SAFE_REPR_MAX_ITEMS
    max_nodes: int = _SAFE_REPR_MAX_NODES
    max_scalar_chars: int = _SAFE_REPR_MAX_SCALAR_CHARS
    _active_ids: set[int] = field(default_factory=set, init=False)
    _rendered_nodes: int = field(default=0, init=False)

    def render(self, value: Any, *, depth: int = 0) -> str:
        if self._rendered_nodes >= self.max_nodes:
            return _SAFE_REPR_OMITTED
        self._rendered_nodes += 1
        if not _is_structured_value(value):
            return self._render_scalar_or_identity(value)
        if depth >= self.max_depth:
            return _SAFE_REPR_OMITTED

        value_id = id(value)
        if value_id in self._active_ids:
            return _SAFE_REPR_OMITTED
        self._active_ids.add(value_id)
        try:
            return self._render_structured(value, depth=depth)
        finally:
            self._active_ids.remove(value_id)

    def _render_structured(self, value: Any, *, depth: int) -> str:
        if is_dataclass(value) and not isinstance(value, type):
            return self._render_dataclass(value, depth=depth)
        if isinstance(value, dict):
            return self._render_dict(value, depth=depth)
        if isinstance(value, list):
            return self._render_list(value, depth=depth)
        if isinstance(value, tuple):
            return self._render_tuple(value, depth=depth)
        return self._render_set(value, depth=depth)

    def _render_dataclass(self, value: Any, *, depth: int) -> str:
        dataclass_fields = dc_fields(value)
        selected = dataclass_fields[: self.max_items]
        parts = [self._render_dataclass_field(value, item, depth=depth) for item in selected]
        if len(dataclass_fields) > self.max_items:
            parts.append(_SAFE_REPR_OMITTED)
        class_name = _truncate_middle(
            redact_text(_type_short_name(type(value))), self.max_scalar_chars
        )
        return f"{class_name}({', '.join(parts)})"

    def _render_dataclass_field(self, value: Any, dataclass_field: Any, *, depth: int) -> str:
        name = dataclass_field.name
        rendered_name = _truncate_middle(name, self.max_scalar_chars)
        if _is_secret_name(name):
            return f"{rendered_name}='***'"
        try:
            field_value = getattr(value, name)
        except Exception:
            rendered = _SAFE_REPR_UNAVAILABLE
        else:
            rendered = self.render(field_value, depth=depth + 1)
        return f"{rendered_name}={rendered}"

    def _render_dict(self, value: dict[Any, Any], *, depth: int) -> str:
        selected, omitted = self._limited(dict.items(value))
        parts: list[str] = []
        for key, item in selected:
            rendered_key = self._render_scalar_or_identity(key)
            rendered_value = (
                "'***'" if _is_secret_mapping_key(key) else self.render(item, depth=depth + 1)
            )
            parts.append(f"{rendered_key}: {rendered_value}")
        if omitted:
            parts.append(_SAFE_REPR_OMITTED)
        return "{" + ", ".join(parts) + "}"

    def _render_list(self, value: list[Any], *, depth: int) -> str:
        selected, omitted = self._limited(list.__iter__(value))
        parts = [self.render(item, depth=depth + 1) for item in selected]
        if omitted:
            parts.append(_SAFE_REPR_OMITTED)
        return "[" + ", ".join(parts) + "]"

    def _render_tuple(self, value: tuple[Any, ...], *, depth: int) -> str:
        selected, omitted = self._limited(tuple.__iter__(value))
        parts = [self.render(item, depth=depth + 1) for item in selected]
        if omitted:
            parts.append(_SAFE_REPR_OMITTED)
        inner = ", ".join(parts)
        return f"({inner},)" if tuple.__len__(value) == 1 else f"({inner})"

    def _render_set(self, value: set[Any] | frozenset[Any], *, depth: int) -> str:
        iterator = set.__iter__(value) if isinstance(value, set) else frozenset.__iter__(value)
        selected, omitted = self._limited(iterator)
        parts = sorted(self.render(item, depth=depth + 1) for item in selected)
        if omitted:
            parts.append(_SAFE_REPR_OMITTED)
        inner = ", ".join(parts)
        if isinstance(value, frozenset):
            return f"frozenset({{{inner}}})" if parts else "frozenset()"
        return "{" + inner + "}" if parts else "set()"

    def _render_scalar_or_identity(self, value: Any) -> str:
        if isinstance(value, Enum):
            rendered = f"{_type_short_name(type(value))}.{value.name}"
        elif isinstance(value, type):
            rendered = f"<class {_type_name(value)}>"
        elif type(value) is str and len(value) > self.max_scalar_chars:
            rendered = f"<str {len(value)} chars>"
        elif type(value) is str:
            rendered = repr(value)
        elif type(value) is int and value.bit_length() > self.max_scalar_chars * 4:
            rendered = f"<int {value.bit_length()} bits>"
        elif type(value) in (int, float, bool, type(None)):
            rendered = repr(value)
        else:
            rendered = f"<{_type_name(type(value))} object>"
        return _truncate_middle(redact_text(rendered), self.max_scalar_chars)

    def _limited(self, values: Iterable[Any]) -> tuple[list[Any], bool]:
        selected = list(islice(values, self.max_items + 1))
        omitted = len(selected) > self.max_items
        return selected[: self.max_items], omitted


def _is_structured_value(value: Any) -> bool:
    return (is_dataclass(value) and not isinstance(value, type)) or isinstance(
        value,
        (dict, list, tuple, set, frozenset),
    )


def _type_name(value_type: type[Any]) -> str:
    try:
        module = type.__getattribute__(value_type, "__module__")
        qualname = type.__getattribute__(value_type, "__qualname__")
    except Exception:
        return "unknown"
    if type(module) is not str or type(qualname) is not str:
        return "unknown"
    return f"{module}.{qualname}"


def _type_short_name(value_type: type[Any]) -> str:
    try:
        name = type.__getattribute__(value_type, "__name__")
    except Exception:
        return "unknown"
    return name if type(name) is str else "unknown"


def _is_secret_mapping_key(value: Any) -> bool:
    return isinstance(value, str) and _is_secret_name(str.__str__(value))


def _truncate_middle(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    if max_chars <= 0:
        return ""
    if max_chars <= len(_SAFE_REPR_OMITTED):
        return _SAFE_REPR_OMITTED[:max_chars]
    available = max_chars - len(_SAFE_REPR_OMITTED)
    prefix_chars = (available + 1) // 2
    suffix_chars = available - prefix_chars
    suffix = value[-suffix_chars:] if suffix_chars else ""
    return f"{value[:prefix_chars]}{_SAFE_REPR_OMITTED}{suffix}"


def safe_config_snapshot(config: object) -> dict[str, Any]:
    """Return a dict containing only allowlisted, non-secret config fields.

    Accepts any object (typically ``EasyConfig`` or ``SessionConfig``).
    Fields are serialised via :func:`_safe_repr` which redacts secret
    fields in nested dataclass values (e.g. provider configs that contain
    ``api_key``) and avoids calling arbitrary object reprs.
    """
    result: dict[str, Any] = {}
    for name in SAFE_CONFIG_FIELDS:
        if _is_secret_name(name):
            continue
        try:
            val = getattr(config, name, None)
        except Exception:
            result[name] = _SAFE_REPR_UNAVAILABLE
            continue
        if val is not None:
            result[name] = _safe_repr(val)
    return result


def _sanitize_url(raw: str) -> str:
    """Reduce a URL to ``scheme://host`` so credentials/query params are stripped."""
    try:
        parsed = urlparse(raw)
        scheme = parsed.scheme or "unknown"
        host = parsed.hostname or "unknown"
        return f"{scheme}://{host}"
    except Exception:
        return "<redacted>"


def safe_env_snapshot() -> dict[str, str]:
    """Return a dict of allowlisted environment variables that are set.

    URL-valued vars (``EASYCAT_LIBSQL_URL``, etc.) are reduced to
    ``scheme://host`` to avoid leaking embedded credentials or signed
    query parameters.
    """
    result: dict[str, str] = {}
    for var in SAFE_ENV_VARS:
        if var not in os.environ:
            continue
        if var in _URL_VALUED_VARS:
            result[var] = _sanitize_url(os.environ[var])
        else:
            result[var] = os.environ[var]
    return result


# ── Write filter hook ─────────────────────────────────────────────


def apply_write_filter(
    record: JournalRecord,
    *,
    redaction: RedactionPolicy = "secrets",
) -> JournalRecord:
    """Journal write-filter hook.

    The default ``"secrets"`` policy scrubs credentials while preserving
    replay-relevant customer content. ``"pii"`` additionally removes phone
    numbers, URLs, request IDs, home paths, and unsafe text fields. Neither
    mode makes a raw journal safe to share without export-time redaction.
    """
    redacted_data = redact_value(record.data, policy=redaction)
    if not isinstance(redacted_data, dict):
        redacted_data = {}
    redacted_error = _redact_error(record.error, redaction=redaction)
    if (
        not record.name.startswith("app.")
        and redacted_data == record.data
        and redacted_error == record.error
    ):
        return record
    # Application records retain the rebuilt snapshot even when equal to the
    # input so later caller mutations cannot rewrite an in-memory fact.
    return replace(record, data=redacted_data, error=redacted_error)


def _redact_error(
    error: ErrorInfo | None,
    *,
    redaction: RedactionPolicy,
) -> ErrorInfo | None:
    if error is None:
        return None
    redacted = ErrorInfo(
        type=redact_text(error.type, policy=redaction),
        message=redact_text(error.message, policy=redaction),
        traceback=(
            redact_text(error.traceback, policy=redaction) if error.traceback is not None else None
        ),
        notes=(redact_text(error.notes, policy=redaction) if error.notes is not None else None),
        children=tuple(
            child
            for child in (_redact_error(child, redaction=redaction) for child in error.children)
            if child is not None
        ),
    )
    return error if redacted == error else redacted


# ── Dev-only banner ──────────────────────────────────────────────

DEV_BUNDLE_BANNER: str = (
    "Contains raw transcripts, tool args, and provider payloads. "
    "Safe to share with your own team in dev; do not upload to "
    "third-party services or attach to public issues until redaction "
    "policy is configured."
)
