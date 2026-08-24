"""Validation environment metadata and runtime-secret policy."""

from __future__ import annotations

import os
import platform
import sys
from typing import Any

from easycat._credentials import has_usable_credential

PROVIDER_ENV_VARS = (
    "OPENAI_API_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
    "CARTESIA_API_KEY",
)

# Credential-bearing environment variables used by EasyCat's built-in
# providers, transports, server auth, durable journal backends, and maintained
# examples/deployment paths.  Keep this separate from ``PROVIDER_ENV_VARS``:
# validation reports intentionally expose presence booleans only for provider
# credentials, while artifact redaction must cover every runtime secret.
#
# Harmless identifiers such as ``TWILIO_ACCOUNT_SID``, ``TURN_USERNAME``, and
# ``AWS_ACCESS_KEY_ID`` are deliberately absent.
RUNTIME_SECRET_ENV_VARS = (
    *PROVIDER_ENV_VARS,
    "EASYCAT_LIBSQL_AUTH_TOKEN",
    "EASYCAT_REMOTE_AGENT_API_KEY",
    "EASYCAT_SERVE_TOKEN",
    "EASYCAT_SUPERVISOR_TOKEN",
    "EASYCAT_WS_TOKEN",
    "LITESTREAM_SECRET_ACCESS_KEY",
    "SIGNALING_AUTH_TOKEN",
    "TELNYX_API_KEY",
    "TELNYX_STREAM_TOKEN_SECRET",
    "TURN_CREDENTIAL",
    "TURN_PASSWORD",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_CALL_API_TOKEN",
    "TWILIO_STREAM_TOKEN_SECRET",
    "WEBRTC_SIGNALING_TOKEN",
)


def runtime_secret_values() -> tuple[str, ...]:
    """Return configured EasyCat secrets that must be redacted from artifacts."""
    values: list[str] = []
    for name in RUNTIME_SECRET_ENV_VARS:
        value = os.environ.get(name)
        if has_usable_credential(value):
            # ``os.environ`` values are strings; keep the assertion explicit
            # because the canonical predicate intentionally accepts object.
            assert isinstance(value, str)
            values.append(value)
    return tuple(values)


def is_ci() -> bool:
    """Return whether validation is running in a conventional CI environment."""
    return os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}


def validation_environment_metadata() -> dict[str, Any]:
    """Build the shared environment block used by validation artifacts."""
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "ci": is_ci(),
        "env_vars": {name: bool(os.environ.get(name)) for name in PROVIDER_ENV_VARS},
    }
