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
        "EASYCAT_DEBUG",
        "EASYCAT_DATA_DIR",
        "EASYCAT_LEGACY_OBS_DUAL_WRITE",
    }
)


# ── Snapshot helpers ──────────────────────────────────────────────


def _is_secret_name(name: str) -> bool:
    lower = name.lower()
    return any(frag in lower for frag in _SECRET_FRAGMENTS)


def safe_config_snapshot(config: object) -> dict[str, Any]:
    """Return a dict containing only allowlisted, non-secret config fields.

    Accepts any object (typically ``EasyCatConfig`` or ``SessionConfig``).
    Fields are serialised as ``repr(value)`` to avoid leaking complex
    objects — full typed snapshots are a future peripheral.
    """
    result: dict[str, Any] = {}
    # Support both dataclasses and plain objects.
    try:
        names = [f.name for f in dc_fields(config)]
    except TypeError:
        names = list(vars(config))
    for name in names:
        if name in SAFE_CONFIG_FIELDS and not _is_secret_name(name):
            val = getattr(config, name, None)
            result[name] = repr(val)
    return result


def safe_env_snapshot() -> dict[str, str]:
    """Return a dict of allowlisted environment variables that are set."""
    return {var: os.environ[var] for var in SAFE_ENV_VARS if var in os.environ}


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
