"""Config and environment safety defaults.

Hard-coded allowlists that prevent secrets from reaching the journal or
artifact store.  The ``apply_write_filter`` hook is a no-op in this
workstream; ``peripheral-redaction.md`` layers a full ``RedactionPolicy``
onto it later.
"""

from __future__ import annotations

import os
from dataclasses import fields as dc_fields
from typing import Any
from urllib.parse import urlparse

from easycat.runtime.records import JournalRecord

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
        "timeouts",
        "debug",
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
    """repr() that redacts secret-looking fields in nested dataclasses.

    For plain scalars this is just ``repr(val)``.  For dataclass values
    it rebuilds a repr string with secret fields (api_key, token, …)
    replaced by ``'***'``.
    """
    try:
        nested = dc_fields(val)
    except TypeError:
        return repr(val)
    parts: list[str] = []
    for f in nested:
        if _is_secret_name(f.name):
            parts.append(f"{f.name}='***'")
        else:
            parts.append(f"{f.name}={repr(getattr(val, f.name))}")
    return f"{type(val).__name__}({', '.join(parts)})"


def safe_config_snapshot(config: object) -> dict[str, Any]:
    """Return a dict containing only allowlisted, non-secret config fields.

    Accepts any object (typically ``EasyCatConfig`` or ``SessionConfig``).
    Fields are serialised via :func:`_safe_repr` which redacts secret
    fields in nested dataclass values (e.g. provider configs that contain
    ``api_key``).
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
    """No-op in WS1.  Future ``RedactionPolicy`` plugs in here."""
    return record


# ── Dev-only banner ──────────────────────────────────────────────

DEV_BUNDLE_BANNER: str = (
    "Contains raw transcripts, tool args, and provider payloads. "
    "Safe to share with your own team in dev; do not upload to "
    "third-party services or attach to public issues until redaction "
    "policy is configured."
)
