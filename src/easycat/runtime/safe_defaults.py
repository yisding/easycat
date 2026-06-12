"""Config, environment, and journal safety defaults.

Hard-coded allowlists keep obvious credentials out of generated config and
environment metadata. The journal write filter also scrubs secret-looking
fields and sensitive substrings, but it deliberately preserves normal
transcript, agent-output, and tool-result text so replay remains useful.
Journal records and exported bundles therefore remain sensitive.
"""

from __future__ import annotations

import os
from dataclasses import fields as dc_fields
from dataclasses import is_dataclass, replace
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from easycat.runtime.records import ErrorInfo, JournalRecord
from easycat.validation.redaction import redact_text, redact_value

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
        "latency_budget",
        "warmup",
        "max_session_cost_usd",
        # Pipeline flags
        "enable_noise_reduction",
        "enable_echo_cancellation",
        "enable_vad",
        "auto_turn_from_stt_final",
        "strip_markdown",
        "interruption_mode",
        # Journal config (safe to report)
        "journal_backend",
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


# ── Snapshot helpers ──────────────────────────────────────────────


def _is_secret_name(name: str) -> bool:
    lower = name.lower()
    return any(frag in lower for frag in _SECRET_FRAGMENTS)


def _safe_repr(val: Any) -> str:
    """Render a config value without invoking repr() on arbitrary objects.

    For scalars this uses ``repr()`` with sensitive substrings redacted. For
    dataclass / dict / list / tuple / set values it walks the structure and
    replaces secret fields (api_key, token, …) with ``'***'`` before rendering.
    Unknown live objects are reduced to their type identity so provider
    instances with credential-bearing ``__repr__`` methods cannot leak secrets
    into exported snapshots.
    """

    def render_scalar_or_identity(v: Any) -> str:
        if isinstance(v, str | int | float | bool | type(None)):
            return redact_text(repr(v))
        if isinstance(v, Enum):
            return f"{type(v).__name__}.{v.name}"
        if isinstance(v, type):
            return f"<class {v.__module__}.{v.__qualname__}>"
        return f"<{type(v).__module__}.{type(v).__qualname__} object>"

    def render(v: Any) -> str:
        # Dataclass instance (but not a dataclass *type*): rebuild a repr
        # string with secret fields redacted and other fields recursed into.
        if is_dataclass(v) and not isinstance(v, type):
            parts: list[str] = []
            for f in dc_fields(v):
                if _is_secret_name(f.name):
                    parts.append(f"{f.name}='***'")
                else:
                    parts.append(f"{f.name}={render(getattr(v, f.name))}")
            return f"{type(v).__name__}({', '.join(parts)})"
        if isinstance(v, dict):
            items = []
            for k, item in v.items():
                if isinstance(k, str) and _is_secret_name(k):
                    items.append(f"{render_scalar_or_identity(k)}: '***'")
                else:
                    items.append(f"{render_scalar_or_identity(k)}: {render(item)}")
            return "{" + ", ".join(items) + "}"
        if isinstance(v, list):
            return "[" + ", ".join(render(item) for item in v) + "]"
        if isinstance(v, tuple):
            inner = ", ".join(render(item) for item in v)
            return f"({inner},)" if len(v) == 1 else f"({inner})"
        if isinstance(v, (set, frozenset)):
            return "{" + ", ".join(render(item) for item in v) + "}"
        return render_scalar_or_identity(v)

    return render(val)


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
        val = getattr(config, name, None)
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


def apply_write_filter(record: JournalRecord) -> JournalRecord:
    """Journal write-filter hook.

    Scrubs secret-looking keyed values plus obvious sensitive substrings from
    record data and error payloads. Normal ``data["text"]`` stays intact for
    replay/debuggability, so this is not a full privacy redaction boundary.
    """
    redacted_data = redact_value(record.data)
    if not isinstance(redacted_data, dict):
        redacted_data = {}
    redacted_error = _redact_error(record.error)
    if redacted_data == record.data and redacted_error is record.error:
        return record
    return replace(record, data=redacted_data, error=redacted_error)


def _redact_error(error: ErrorInfo | None) -> ErrorInfo | None:
    if error is None:
        return None
    redacted = ErrorInfo(
        type=redact_text(error.type),
        message=redact_text(error.message),
        traceback=redact_text(error.traceback) if error.traceback is not None else None,
        notes=redact_text(error.notes) if error.notes is not None else None,
        children=tuple(
            child
            for child in (_redact_error(child) for child in error.children)
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
